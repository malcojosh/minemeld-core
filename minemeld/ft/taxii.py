#  Copyright 2015 Palo Alto Networks, Inc
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import absolute_import

import logging
import copy
import urlparse
import datetime
import pytz
import os.path
import lxml.etree
import yaml
import uuid
import redis
import gevent
import gevent.event

import libtaxii
import libtaxii.clients
import libtaxii.messages_11

import stix.core.stix_package
import stix.indicator
import stix.common.vocabs

import cybox.core
import cybox.objects.address_object
import cybox.objects.domain_name_object
import cybox.objects.uri_object

from . import basepoller
from . import base
from .utils import dt_to_millisec, interval_in_sec, utc_millisec

LOG = logging.getLogger(__name__)


class TaxiiClient(basepoller.BasePollerFT):
    def __init__(self, name, chassis, config):
        self.poll_service = None
        self.collection_mgmt_service = None
        self.last_taxii_run = None

        super(TaxiiClient, self).__init__(name, chassis, config)

    def configure(self):
        super(TaxiiClient, self).configure()

        self.initial_interval = self.config.get('initial_interval', '1d')
        self.initial_interval = interval_in_sec(self.initial_interval)
        if self.initial_interval is None:
            LOG.error(
                '%s - wrong initial_interval format: %s',
                self.name, self.initial_interval
            )
            self.initial_interval = 86400

        self.discovery_service = self.config.get('discovery_service', None)
        self.username = self.config.get('username', None)
        self.password = self.config.get('password', None)
        self.collection = self.config.get('collection', None)
        self.prefix = self.config.get('prefix', self.name)
        self.ca_file = self.config.get('ca_file', None)

        self.key_file = self.config.get('key_file', None)
        if isinstance(self.key_file, bool) and self.key_file:
            self.key_file = os.path.join(
                os.environ['MM_CONFIG_DIR'],
                '%s-key.pem' % self.name
            )

        self.cert_file = self.config.get('cert_file', None)
        if isinstance(self.cert_file, bool) and self.cert_file:
            self.cert_file = os.path.join(
                os.environ['MM_CONFIG_DIR'],
                '%s-key.pem' % self.name
            )

        self.side_config_path = self.config.get('side_config', None)
        if self.side_config_path is None:
            self.side_config_path = os.path.join(
                os.environ['MM_CONFIG_DIR'],
                '%s_side_config.yml' % self.name
            )

        self.confidence_map = self.config.get('confidence_map', {
            'low': 40,
            'medium': 60,
            'high': 80
        })

        self._load_side_config()

    def _load_side_config(self):
        try:
            with open(self.side_config_path, 'r') as f:
                sconfig = yaml.safe_load(f)

        except Exception as e:
            LOG.error('%s - Error loading side config: %s', self.name, str(e))
            return

        username = sconfig.get('username', None)
        password = sconfig.get('password', None)
        if username is not None and password is not None:
            self.username = username
            self.password = password
            LOG.info('Loaded credentials from side config')

    def _build_taxii_client(self):
        result = libtaxii.clients.HttpClient()

        up = urlparse.urlparse(self.discovery_service)

        if up.scheme == 'https':
            result.set_use_https(True)

        if self.username and self.password:
            if self.key_file and self.cert_file:
                result.set_auth_type(
                    libtaxii.clients.HttpClient.AUTH_CERT_BASIC
                )
                result.set_auth_credentials({
                    'username': self.username,
                    'password': self.password,
                    'key_file': self.key_file,
                    'cert_file': self.cert_file
                })

            else:
                result.set_auth_type(
                    libtaxii.clients.HttpClient.AUTH_BASIC
                )
                result.set_auth_credentials({
                    'username': self.username,
                    'password': self.password
                })

        else:
            if self.key_file and self.cert_file:
                result.set_auth_type(
                    libtaxii.clients.HttpClient.AUTH_CERT
                )
                result.set_auth_credentials({
                    'key_file': self.key_file,
                    'cert_file': self.cert_file
                })

            else:
                result.set_auth_type(
                    libtaxii.clients.HttpClient.AUTH_NONE
                )

        if self.ca_file is not None:
            result.set_verify_server(
                verify_server=True,
                ca_file=self.ca_file
            )

        return result

    def _discover_services(self, tc):
        msg_id = libtaxii.messages_11.generate_message_id()
        request = libtaxii.messages_11.DiscoveryRequest(msg_id)
        request = request.to_xml()

        up = urlparse.urlparse(self.discovery_service)
        hostname = up.hostname
        path = up.path

        resp = tc.call_taxii_service2(
            hostname,
            path,
            libtaxii.constants.VID_TAXII_XML_11,
            request
        )

        tm = libtaxii.get_message_from_http_response(resp, msg_id)

        LOG.debug('Discovery_Response {%s} %s',
                  type(tm), tm.to_xml(pretty_print=True))

        self.collection_mgmt_service = None
        for si in tm.service_instances:
            if si.service_type != libtaxii.constants.SVC_COLLECTION_MANAGEMENT:
                continue

            self.collection_mgmt_service = si.service_address
            break

        if self.collection_mgmt_service is None:
            raise RuntimeError('%s - collection management service not found' %
                               self.name)

    def _check_collections(self, tc):
        msg_id = libtaxii.messages_11.generate_message_id()
        request = libtaxii.messages_11.CollectionInformationRequest(msg_id)
        request = request.to_xml()

        up = urlparse.urlparse(self.collection_mgmt_service)
        hostname = up.hostname
        path = up.path

        resp = tc.call_taxii_service2(
            hostname,
            path,
            libtaxii.constants.VID_TAXII_XML_11,
            request
        )

        tm = libtaxii.get_message_from_http_response(resp, msg_id)

        LOG.debug('Collection_Information_Response {%s} %s',
                  type(tm), tm.to_xml(pretty_print=True))

        tci = None
        for ci in tm.collection_informations:
            if ci.collection_name != self.collection:
                continue

            tci = ci
            break

        if tci is None:
            raise RuntimeError('%s - collection %s not found' %
                               (self.name, self.collection))

        if tci.polling_service_instances is None or \
           len(tci.polling_service_instances) == 0:
            raise RuntimeError('%s - collection %s doesn\'t support polling' %
                               (self.name, self.collection))

        if tci.collection_type != libtaxii.constants.CT_DATA_FEED:
            raise RuntimeError(
                '%s - collection %s is not a data feed (%s)' %
                (self.name, self.collection, tci.collection_type)
            )

        self.poll_service = tci.polling_service_instances[0].poll_address

        LOG.debug('%s - poll service: %s',
                  self.name, self.poll_service)

    def _poll_fulfillment_request(self, tc, result_id, result_part_number):
        msg_id = libtaxii.messages_11.generate_message_id()
        request = libtaxii.messages_11.PollFulfillmentRequest(
            message_id=msg_id,
            result_id=result_id,
            result_part_number=result_part_number,
            collection_name=self.collection
        )
        request = request.to_xml()

        up = urlparse.urlparse(self.poll_service)
        hostname = up.hostname
        path = up.path

        resp = tc.call_taxii_service2(
            hostname,
            path,
            libtaxii.constants.VID_TAXII_XML_11,
            request
        )

        return libtaxii.get_message_from_http_response(resp, msg_id)

    def _poll_collection(self, tc, begin=None, end=None):
        msg_id = libtaxii.messages_11.generate_message_id()
        pps = libtaxii.messages_11.PollParameters(
            response_type='FULL',
            allow_asynch=False
        )
        request = libtaxii.messages_11.PollRequest(
            message_id=msg_id,
            collection_name=self.collection,
            exclusive_begin_timestamp_label=begin,
            inclusive_end_timestamp_label=end,
            poll_parameters=pps
        )

        LOG.debug('%s - first poll request %s',
                  self.name, request.to_xml(pretty_print=True))

        request = request.to_xml()

        up = urlparse.urlparse(self.poll_service)
        hostname = up.hostname
        path = up.path

        resp = tc.call_taxii_service2(
            hostname,
            path,
            libtaxii.constants.VID_TAXII_XML_11,
            request
        )

        tm = libtaxii.get_message_from_http_response(resp, msg_id)

        LOG.debug('%s - Poll_Response {%s} %s',
                  self.name, type(tm), tm.to_xml(pretty_print=True))

        stix_objects = {
            'observables': {},
            'indicators': {},
            'ttps': {}
        }

        self._handle_content_blocks(
            tm.content_blocks,
            stix_objects
        )

        while tm.more:
            tm = self._poll_fulfillment_request(
                tc,
                result_id=tm.result_id,
                result_part_number=tm.result_part_number+1
            )
            self._handle_content_blocks(
                tm.content_blocks,
                stix_objects
            )

        LOG.debug('%s - stix_objects: %s', self.name, stix_objects)

        params = {
            'ttps': stix_objects['ttps'],
            'observables': stix_objects['observables']
        }
        return [[iid, iv, params]
                for iid, iv in stix_objects['indicators'].iteritems()]

    def _handle_content_blocks(self, content_blocks, objects):
        try:
            for cb in content_blocks:
                if cb.content_binding.binding_id != \
                   libtaxii.constants.CB_STIX_XML_111:
                    LOG.error('%s - Unsupported content binding: %s',
                              self.name, cb.content_binding.binding_id)
                    continue

                try:
                    stixpackage = stix.core.stix_package.STIXPackage.from_xml(
                        lxml.etree.fromstring(cb.content)
                    )
                except Exception:
                    LOG.exception(
                        '%s - Exception parsing contnet block',
                        self.name
                    )
                    continue

                if stixpackage.indicators:
                    for i in stixpackage.indicators:
                        ci = {
                            'timestamp': dt_to_millisec(i.timestamp),
                        }

                        if i.confidence is not None:
                            confidence = str(i.confidence.value).lower()
                            if confidence in self.confidence_map:
                                ci['confidence'] = \
                                    self.confidence_map[confidence]

                        os = []
                        ttps = []

                        if i.observable:
                            os.append(self._decode_observable(i.observable))
                        if i.observables:
                            for o in i.observables:
                                os.append(self._decode_observable(o))
                        if i.indicated_ttps:
                            for t in i.indicated_ttps:
                                ttps.append(self._decode_ttp(t))

                        ci['observables'] = os
                        ci['ttps'] = ttps

                        objects['indicators'][i.id_] = ci

                if stixpackage.observables:
                    for o in stixpackage.observables:
                        co = self._decode_observable(o)
                        objects['observables'][o.id_] = co

                if stixpackage.ttps:
                    for t in stixpackage.ttps:
                        ct = self._decode_ttp(t)
                        objects['ttps'][t.id_] = ct

        except:
            LOG.exception("%s - exception in _handle_content_blocks" %
                          self.name)
            raise

    def _decode_observable(self, o):
        LOG.debug('observable: %s', o.to_dict())

        if o.idref:
            return {'idref': o.idref}

        odict = o.to_dict()

        oc = odict.get('observable_composition', None)
        if oc:
            LOG.error('%s - Observable composition not supported yet: %s',
                      self.name, odict)
            return None

        oo = odict.get('object', None)
        if oo is None:
            LOG.error('%s - no object in observable', self.name)
            return None

        op = oo.get('properties', None)
        if op is None:
            LOG.error('%s - no properties in observable object', self.name)
            return None

        ot = op.get('xsi:type', None)
        if ot is None:
            LOG.error('%s - no type in observable props', self.name)
            return None

        result = {}

        if ot == 'DomainNameObjectType':
            result['type'] = 'domain'

            ov = op.get('value', None)
            if ov is None:
                LOG.error('%s - no value in observable props', self.name)
                return None
            if type(ov) != str:
                ov = ov.get('value', None)
                if ov is None:
                    LOG.error('%s - no value in observable value', self.name)
                    return None

        elif ot == 'AddressObjectType':
            addrcat = op.get('category', None)
            if addrcat == 'ipv6-addr':
                result['type'] = 'IPv6'
            result['type'] = 'IPv4'

            source = op.get('is_source', None)
            if source is True:
                result['direction'] = 'inbound'
            elif source is False:
                result['direction'] = 'outbound'

            ov = op.get('address_value', None)
            if ov is None:
                LOG.error('%s - no value in observable props', self.name)
                return None
            if type(ov) != str:
                ov = ov.get('value', None)
                if ov is None:
                    LOG.error('%s - no value in observable value', self.name)
                    return None

        elif ot == 'URIObjectType':
            result['type'] = 'URL'

            ov = op.get('value', None)
            if ov is None:
                LOG.error('%s - no value in observable props', self.name)
                return None
            if type(ov) != str:
                ov = ov.get('value', None)
                if ov is None:
                    LOG.error('%s - no value in observable value', self.name)
                    return None

        else:
            LOG.error('%s - unknown type %s', self.name, ot)
            return None

        result['indicator'] = ov

        return result

    def _decode_ttp(self, t):
        tdict = t.to_dict()

        if 'ttp' in tdict:
            tdict = tdict['ttp']

        if 'idref' in tdict:
            return {'idref': tdict['idref']}

        if 'description' in tdict:
            return {'description': tdict['description']}

        if 'title' in tdict:
            return {'description': tdict['title']}

        return {'description': ''}

    def _process_item(self, item):
        result = []
        value = {}

        iid, iv, stix_objects = item

        value['%s_indicator' % self.prefix] = iid

        if 'confidence' in iv:
            value['confidence'] = iv['confidence']

        if len(iv['ttps']) != 0:
            ttp = iv['ttps'][0]
            if 'idref' in ttp:
                ttp = stix_objects['ttps'].get(ttp['idref'])

            if ttp is not None and 'description' in ttp:
                value['%s_ttp' % self.prefix] = ttp['description']

        for o in iv['observables']:
            v = copy.copy(value)

            ob = o
            if 'idref' in o:
                ob = stix_objects['observables'].get(o['idref'], None)
                v['%s_observable' % self.prefix] = o['idref']

            if ob is None:
                continue

            v['type'] = ob['type']

            if type(ob['indicator']) == list:
                indicator = ob['indicator']
            else:
                indicator = [ob['indicator']]

            for i in indicator:
                result.append([i, v])

        return result

    def _build_iterator(self, now):
        tc = self._build_taxii_client()
        self._discover_services(tc)
        self._check_collections(tc)

        last_run = self.last_taxii_run
        if last_run is None:
            last_run = now-(self.initial_interval*1000)

        begin = datetime.datetime.fromtimestamp(last_run/1000)
        begin = begin.replace(tzinfo=pytz.UTC)

        end = datetime.datetime.fromtimestamp(now/1000)
        end = end.replace(tzinfo=pytz.UTC)

        result = self._poll_collection(
            tc,
            begin=begin,
            end=end
        )

        self.last_taxii_run = now

        return result

    def hup(self, source=None):
        LOG.info('%s - hup received, reload side config', self.name)
        self._load_side_config()
        super(TaxiiClient, self).hup(source)


def _stix_ip_observable(id_, indicator, value):
    category = cybox.objects.address_object.Address.CAT_IPV4
    if value['type'] == 'IPv6':
        category = cybox.objects.address_object.Address.CAT_IPV6

    ao = cybox.objects.address_object.Address(
        address_value=indicator,
        category=category
    )

    o = cybox.core.Observable(
        title='{}: {}'.format(value['type'], indicator),
        id_=id_,
        item=ao
    )

    return o


def _stix_domain_observable(id_, indicator, value):
    do = cybox.objects.domain_name_object.DomainName(
        value=indicator,
        type_="FQDN"
    )

    o = cybox.core.Observable(
        title='FQDN: ' + indicator,
        id_=id_,
        item=do
    )

    return o


def _stix_url_observable(id_, indicator, value):
    uo = cybox.objects.uri_object.URI(
        value=indicator,
        type_=cybox.objects.uri_object.URI.TYPE_URL
    )

    o = cybox.core.Observable(
        title='URL: ' + indicator,
        id_=id_,
        item=uo
    )

    return o


_TYPE_MAPPING = {
    'IPv4': {
        'indicator_type': stix.common.vocabs.IndicatorType.TERM_IP_WATCHLIST,
        'mapper': _stix_ip_observable
    },
    'IPv6': {
        'indicator_type': stix.common.vocabs.IndicatorType.TERM_IP_WATCHLIST,
        'mapper': _stix_ip_observable
    },
    'URL': {
        'indicator_type': stix.common.vocabs.IndicatorType.TERM_DOMAIN_WATCHLIST,
        'mapper': _stix_url_observable
    },
    'domain': {
        'indicator_type': stix.common.vocabs.IndicatorType.TERM_URL_WATCHLIST,
        'mapper': _stix_domain_observable
    }
}


class DataFeed(base.BaseFT):
    def __init__(self, name, chassis, config):
        self.redis_skey = name
        self.redis_skey_value = name+'.value'
        self.redis_skey_chkp = name+'.chkp'

        self.SR = None
        self.ageout_glet = None

        super(DataFeed, self).__init__(name, chassis, config)

    def configure(self):
        super(DataFeed, self).configure()

        self.redis_host = self.config.get('redis_host', 'localhost')
        self.redis_port = self.config.get('redis_port', 6379)
        self.redis_password = self.config.get('redis_password', None)
        self.redis_db = self.config.get('redis_db', 0)

        self.namespace = self.config.get('namespace', 'minemeld')
        self.namespaceuri = self.config.get(
            'namespaceuri',
            'https://go.paloaltonetworks.com/minemeld'
        )

        self.age_out_interval = self.config.get('age_out_interval', '24h')
        self.age_out_interval = interval_in_sec(self.age_out_interval)
        if self.age_out_interval < 60:
            LOG.info('%s - age out interval too small, forced to 60 seconds')
            self.age_out_interval = 60

    def connect(self, inputs, output):
        output = False
        super(DataFeed, self).connect(inputs, output)

    def read_checkpoint(self):
        self._connect_redis()
        self.last_checkpoint = self.SR.get(self.redis_skey_chkp)
        self.SR.delete(self.redis_skey_chkp)

    def create_checkpoint(self, value):
        self._connect_redis()
        self.SR.set(self.redis_skey_chkp, value)

    def _connect_redis(self):
        if self.SR is not None:
            return

        self.SR = redis.StrictRedis(
            host=self.redis_host,
            port=self.redis_port,
            password=self.redis_password,
            db=self.redis_db
        )

    def _read_oldest_indicator(self):
        olist = self.SR.zrevrange(
            self.redis_skey, 0, 0,
            withscores=True
        )
        LOG.debug('%s - oldest: %s', self.name, olist)
        if len(olist) == 0:
            return None, None

        return int(olist[0][1]), olist[0][0]

    def initialize(self):
        self._connect_redis()

    def rebuild(self):
        self._connect_redis()
        self.SR.delete(self.redis_skey)
        self.SR.delete(self.redis_skey_value)

    def reset(self):
        self._connect_redis()
        self.SR.delete(self.redis_skey)
        self.SR.delete(self.redis_skey_value)

    def _add_indicator(self, score, id_, indicator, value):
        type_ = value['type']
        type_mapper = _TYPE_MAPPING.get(type_, None)
        if type_mapper is None:
            LOG.error('%s - Unsupported indicator type: %s', self.name, type_)
            return

        nsdict = {}
        nsdict[self.namespaceuri] = self.namespace
        stix.utils.set_id_namespace(nsdict)

        sp = stix.core.STIXPackage()
        sindicator = stix.indicator.indicator.Indicator(
            id_=id_,
            title='{}: {}'.format(
                value['type'],
                indicator
            ),
            description='{} indicator from {}'.format(
                value['type'],
                ', '.join(value['sources'])
            ),
            timestamp=datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
        )

        confidence = value.get('confidence', None)
        if confidence is None:
            LOG.error('%s - indicator without confidence', self.name)
            sindicator.confidence = "Unknown"  # We shouldn't be here
        elif confidence < 50:
            sindicator.confidence = "Low"
        elif confidence < 75:
            sindicator.confidence = "Medium"
        else:
            sindicator.confidence = "High"

        sindicator.add_indicator_type(type_mapper['indicator_type'])

        oid = '{}:observable-{}'.format(
            self.namespace,
            uuid.uuid4()
        )
        sindicator.add_observable(
            type_mapper['mapper'](oid, indicator, value)
        )

        sp.add_indicator(sindicator)

        with self.SR.pipeline() as p:
            p.multi()

            p.zadd(self.redis_skey, score, id_)
            p.hset(self.redis_skey_value, id_, sp.to_xml())

            result = p.execute()[0]

        self.statistics['added'] += result

    def _delete_indicator(self, indicator_id):
        with self.SR.pipeline() as p:
            p.multi()

            p.zrem(self.redis_skey, indicator_id)
            p.hdel(self.redis_skey_value, indicator_id)

            result = p.execute()[0]

        self.statistics['removed'] += result

    def _age_out_run(self):
        while True:
            now = utc_millisec()
            low_watermark = now - self.age_out_interval*1000

            otimestamp, oindicator = self._read_oldest_indicator()
            LOG.debug(
                '{} - low watermark: {} otimestamp: {}'.format(
                    self.name,
                    low_watermark,
                    otimestamp
                )
            )
            while otimestamp is not None and otimestamp < low_watermark:
                self._delete_indicator(oindicator)
                otimestamp, oindicator = self._read_oldest_indicator()

            wait_time = 30
            if otimestamp is not None:
                next_expiration = (
                    (otimestamp + self.age_out_interval*1000) - now
                )
                wait_time = max(wait_time, next_expiration/1000 + 1)
            LOG.debug('%s - sleeping for %d secs', self.name, wait_time)

            gevent.sleep(wait_time)

    @base._counting('update.processed')
    def filtered_update(self, source=None, indicator=None, value=None):
        now = utc_millisec()
        id_ = '{}:indicator-{}'.format(
            self.namespace,
            uuid.uuid4()
        )

        self._add_indicator(now, id_, indicator, value)

    @base._counting('withdraw.ignored')
    def filtered_withdraw(self, source=None, indicator=None, value=None):
        # this is a TAXII data feed, old indicators never expire
        pass

    def length(self, source=None):
        return self.SR.zcard(self.redis_skey)

    def start(self):
        super(DataFeed, self).start()

        self.ageout_glet = gevent.spawn(self._age_out_run)

    def stop(self):
        super(DataFeed, self).stop()

        self.ageout_glet.kill()

        LOG.info(
            "%s - # indicators: %d",
            self.name,
            self.SR.zcard(self.redis_skey)
        )
