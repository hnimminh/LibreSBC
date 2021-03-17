import traceback
import re
import json

import redis
from pydantic import BaseModel, Field, validator, root_validator
from typing import Optional, List, Dict
from enum import Enum
from ipaddress import IPv4Address, IPv4Network
from fastapi import APIRouter, Request, Response, Path
from fastapi.encoders import jsonable_encoder

from configuration import (_APPLICATION, _SWVERSION, _DESCRIPTION, 
                           NODEID, CLUSTERNAME, CLUSTERMEMBERS,
                           SWCODECS, MAX_SPS, MAX_SESSION, 
                           REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD, SCAN_COUNT)
from utilities import logify, debugy, get_request_uuid, int2bool, bool2int, humanrid, redishash, jsonhash, listify, getnameid


REDIS_CONNECTION_POOL = redis.BlockingConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, 
                                                     decode_responses=True, max_connections=10, timeout=5)
rdbconn = redis.StrictRedis(connection_pool=REDIS_CONNECTION_POOL)                                                    
pipe = rdbconn.pipeline()

# PATTERN
_NAME_ = '^[a-zA-Z][a-zA-Z0-9_]+$'
_DIAL_ = '^[a-zA-Z0-9+#*@]*$'
# API ROUTER DECLARATION
librerouter = APIRouter()
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# INITIALIZE
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
try:
    _clustername = rdbconn.get('cluster:name')
    if _clustername: CLUSTERNAME = _clustername
    _clustermembers = set(rdbconn.smembers('cluster:members')) 
    if _clustermembers: CLUSTERMEMBERS = _clustermembers
except:
    pass

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# PREDEFINED INFORMATION
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
@librerouter.get("/libresbc/predefine", status_code=200)
def predefine():
    return {
        "nodeid": NODEID,
        "cluster": {
            "name": CLUSTERNAME,
            "members": CLUSTERMEMBERS
        },
        "application": _APPLICATION,
        'swversion': _SWVERSION,
        "description": _DESCRIPTION
    }

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# CLUSTER & NODE
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
class ClusterModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='The name of libresbc cluster')
    members: List[str] = Field(min_items=1, max_item=16, description='The member of libresbc cluster')


@librerouter.put("/libresbc/cluster", status_code=200)
def update_cluster(reqbody: ClusterModel, response: Response):
    result = None
    try:
        name = reqbody.name
        members = set(reqbody.members)
        _members = set(rdbconn.smembers('cluster:members'))
  
        removed_members = _members - members
        for removed_member in removed_members:
            if rdbconn.scard(f'engagement:node:{removed_member}'):
                response.status_code, result = 403, {'error': 'engaged node'}; return

        pipe.set('cluster:name', name)
        for member in members: pipe.sadd('cluster:members', member)
        pipe.execute()
        CLUSTERNAME, CLUSTERMEMBERS = name, members
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=change_cluster, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.get("/libresbc/cluster", status_code=200)
def get_cluster(response: Response):
    result = None
    try:
        response.status_code, result = 200, {'name': CLUSTERNAME, 'members': CLUSTERMEMBERS}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=get_cluster, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


class ACLActionEnum(str, Enum):
    allow = 'allow'
    deny = 'deny'

class ACLTypeEnum(str, Enum):
    cidr = 'cidr'
    domain = 'domain'

class ACLNodeModel(BaseModel):
    action: ACLActionEnum = Field(default='allow', description='associate action for node')
    _type: ACLTypeEnum = Field(default='cidr', description='type of acl node', alias='type')
    value: str = Field(description='node value')

class ACLModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of acl (identifier)')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    default: RuleActionEnum = Field(default='deny', description='default action')
    nodes: List[ACLNodeModel] = Field(min_items=1, max_items=64, description='default action')

@librerouter.post("/libresbc/class/ringtone", status_code=200)
def create_ringtone_class(reqbody: RingtoneModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'class:ringtone:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/class/ringtone/{identifier}", status_code=200)
def update_ringtone_class(reqbody: RingtoneModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'class:ringtone:{identifier}'
        name_key = f'class:ringtone:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(engagement, name)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/class/ringtone/{identifier}", status_code=200)
def delete_ringtone_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:ringtone:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engage_key): 
            response.status_code, result = 403, {'error': 'engaged class'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        pipe.delete(_engage_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/ringtone/{identifier}", status_code=200)
def detail_ringtone_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:ringtone:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engage_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/ringtone", status_code=200)
def list_ringtone_class(response: Response):
    result = None
    try:
        KEYPATTERN = f'class:ringtone:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# SIP PROFILES 
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
class SIPProfileModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='friendly name of sip profile')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    user_agent: str = Field(default='LibreSBC', max_length=64, description='Value that will be displayed in SIP header User-Agent')
    disable_transfer: bool = Field(default=False, description='true mean disable call transfer')
    manual_redirect: bool = Field(default=False, description='how call forward handled, true mean it be controlled under libresbc contraints, false mean it be work automatically')
    disable_hold: bool = Field(default=False, description='no handling the SIP re-INVITE with hold/unhold')
    nonce_ttl: int = Field(default=60, ge=15, le=120, description='TTL for nonce in sip auth')
    nat_space: str = Field(default='rfc1918.auto', description='the network will be applied NAT')
    sip_options_respond_503_on_busy: bool = Field(default=True, description='response 503 when system is in heavy load')
    enable_100rel: bool = Field(default=True, description='Reliability - PRACK message as defined in RFC3262')
    enable_timer: bool = Field(default=True, description='true to support for RFC 4028 SIP Session Timers')
    session_timeout: int = Field(default=0, ge=1800, le=3600, description='call to expire after the specified seconds')
    minimum_session_expires: int = Field(default=120, ge=90, le=3600, description='Value of SIP header Min-SE')
    sip_listen_port: int = Field(default=5060, ge=0, le=65535, description='Port to bind to for SIP traffic')
    sip_listen_ip: IPv4Address = Field(description='IP to bind to for SIP traffic')
    sip_advertising_ip: IPv4Address = Field(description='IP address that used to advertise to public network for SIP')
    rtp_listen_ip: IPv4Address = Field(description='IP to bind to for RTP traffic')
    rtp_advertising_ip: IPv4Address = Field(description='IP address that used to advertise to public network for RTP')
    sip_tls: bool = Field(default=False, description='true to enable SIP TLS')
    sips_port: int = Field(default=5061, ge=0, le=65535, description='Port to bind to for TLS SIP traffic')
    tls_version: str = Field(default='tlsv1.2', description='TLS version')
    tls_cert_dir: str = Field(default='', description='TLS Certificate dirrectory')


@librerouter.post("/libresbc/sipprofile", status_code=200)
def create_sipprofile(reqbody: SIPProfileModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'sipprofile:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent sip profile name'}; return
        rdbconn.hmset(name_key, redishash(data))
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_sipprofile, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/sipprofile/{identifier}", status_code=200)
def update_sipprofile(reqbody: SIPProfileModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'sipprofile:{identifier}'
        name_key = f'sipprofile:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent sip profile identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent sip profile name'}; return
        rdbconn.hmset(name_key, redishash(data))
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(engagement, name)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_sipprofile, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/sipprofile/{identifier}", status_code=200)
def delete_sipprofile(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'sipprofile:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engage_key): 
            response.status_code, result = 403, {'error': 'engaged sipprofile'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent sipprofile'}; return
        pipe.delete(_engage_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_sipprofile, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/sipprofile/{identifier}", status_code=200)
def detail_sipprofile(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'sipprofile:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent sip profile'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engage_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_sipprofile, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/sipprofile", status_code=200)
def list_sipprofile(response: Response):
    result = None
    try:
        KEYPATTERN = f'sipprofile:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_sipprofile, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# RINGTONE CLASS 
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
class RingtoneModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of ringtone class (identifier)')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    data: str = Field(min_length=8, max_length=256, description='ringtone data which can be full-path of audio file or tone script follow ITU-T Recommendation E.180')

@librerouter.post("/libresbc/class/ringtone", status_code=200)
def create_ringtone_class(reqbody: RingtoneModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'class:ringtone:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/class/ringtone/{identifier}", status_code=200)
def update_ringtone_class(reqbody: RingtoneModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'class:ringtone:{identifier}'
        name_key = f'class:ringtone:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(engagement, name)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/class/ringtone/{identifier}", status_code=200)
def delete_ringtone_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:ringtone:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engage_key): 
            response.status_code, result = 403, {'error': 'engaged class'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        pipe.delete(_engage_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/ringtone/{identifier}", status_code=200)
def detail_ringtone_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:ringtone:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engage_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/ringtone", status_code=200)
def list_ringtone_class(response: Response):
    result = None
    try:
        KEYPATTERN = f'class:ringtone:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_ringtone_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# CODEC CLASS 
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
class CodecEnum(str, Enum):
    PCMA = "PCMA"
    PCMU = "PCMU"
    G729 = "G729"

class CodecModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of codec class (identifier)')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    codecs: List[CodecEnum] = Field(min_items=1, max_item=len(SWCODECS), description='sorted list of codec')


@librerouter.post("/libresbc/class/codec", status_code=200)
def create_codec_class(reqbody: CodecModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'class:codec:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_codec_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/class/codec/{identifier}", status_code=200)
def update_codec_class(reqbody: CodecModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'class:codec:{identifier}'
        name_key = f'class:codec:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(engagement, name)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_codec_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/class/codec/{identifier}", status_code=200)
def delete_codec_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:codec:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engage_key): 
            response.status_code, result = 403, {'error': 'engaged class'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        pipe.delete(_engage_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_codec_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/codec/{identifier}", status_code=200)
def detail_codec_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:codec:{identifier}'
        _engage_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engage_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_codec_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/codec", status_code=200)
def list_codec_class(response: Response):
    result = None
    try:
        KEYPATTERN = f'class:codec:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_codec_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# CAPACITY 
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
class CapacityModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of capacity class (identifier)')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    cps: int = Field(default=2, ge=1, le=len(CLUSTERMEMBERS)*MAX_SPS//2, description='call per second')
    ccs: int = Field(default=10, ge=1, le=len(CLUSTERMEMBERS)*MAX_SESSION//2, description='concurrent calls')


@librerouter.post("/libresbc/class/capacity", status_code=200)
def create_capacity_class(reqbody: CapacityModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'class:capacity:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_capacity_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/class/capacity/{identifier}", status_code=200)
def update_capacity_class(reqbody: CapacityModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'class:capacity:{identifier}'
        name_key = f'class:capacity:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, redishash(data))
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(engagement, name)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_capacity_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/class/capacity/{identifier}", status_code=200)
def delete_capacity_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:capacity:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engaged_key): 
            response.status_code, result = 403, {'error': 'engaged class'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        pipe.delete(_engaged_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_capacity_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/capacity/{identifier}", status_code=200)
def detail_capacity_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:capacity:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engaged_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_capacity_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/capacity", status_code=200)
def list_capacity_class(response: Response):
    result = None
    try:
        KEYPATTERN = f'class:capacity:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_capacity_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# NUMBER TRANSLATION 
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------

class TranslationModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of translation class')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    caller_pattern: str = Field(max_length=128, description='caller pattern use pcre')
    callee_pattern: str = Field(max_length=128, description='callee pattern use pcre')
    caller_replacement: str = Field(max_length=128, description='replacement that refer to caller pattern use pcre')
    callee_replacement: str = Field(max_length=128, description='replacement that refer to callee pattern use pcre')

@librerouter.post("/libresbc/class/translation", status_code=200)
def create_translation_class(reqbody: TranslationModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'class:translation:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, data)
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_translation_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/class/translation/{identifier}", status_code=200)
def update_translation_class(reqbody: TranslationModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'class:translation:{identifier}'
        name_key = f'class:translation:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent class name'}; return
        rdbconn.hmset(name_key, data)
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(engagement, name)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_translation_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/class/translation/{identifier}", status_code=200)
def delete_translation_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:translation:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engaged_key): 
            response.status_code, result = 403, {'error': 'engaged class'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        pipe.delete(_engaged_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_translation_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/translation/{identifier}", status_code=200)
def detail_translation_class(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'class:translation:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent class identifier'}; return
        result = rdbconn.hgetall(_name_key)
        engagements = rdbconn.smembers(_engaged_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_translation_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/class/translation", status_code=200)
def list_translation_class(response: Response):
    result = None
    try:
        KEYPATTERN = f'class:translation:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0]})
        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_translation_class, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# GATEWAY
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
class TransportEnum(str, Enum):
    UDP = "UDP"
    TCP = "TCP"
    TLS = "TLS"

class CidTypeEnum(str, Enum):
    none = 'none'
    rpid = 'rpid'
    pidd = 'pid'

class GatewayModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of translation class')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    ip: IPv4Address = Field(description='farend ip address')
    port: int = Field(default=5060, ge=0, le=65535, description='farend destination port')
    transport: TransportEnum = Field(default='UDP', description='farend transport protocol')
    username: Optional[str] = Field(default='', description='digest auth username')
    password: Optional[str] = Field(default='', description='digest auth password')
    realm: Optional[str] = Field(default='', description='digest auth realm')
    from_user: Optional[str] = Field(default='', description='username to use in from')
    from_domain: Optional[str] = Field(default='', description='domain to use in from')
    _register: bool = Field(default=False, description='register', alias='register')
    register_proxy: Optional[str] = Field(default='', description='proxy address to register')
    expire_seconds: Optional[int] = Field(default=1800, ge=60, le=3600, description='register expire interval in second')
    retry_seconds: Optional[int] = Field(default=60, ge=30, le=600, description='interval in second before a retry when a failure or timeout occurs')
    sip_cid_type: CidTypeEnum = Field(default='none', description='caller id type: rpid, pid, none')
    caller_id_in_from: bool = Field(default=False, description='caller id in from hearder')
    healthcheck: bool = Field(default=True, description='healthcheck this gateway by ping SIP OPTION')
    ping: Optional[int] = Field(default=300, ge=5, le=3600, description='the period (second) to send SIP OPTION')
    ping_max: Optional[int] = Field(default=1, ge=1, le=31, description='number of success pings to declaring a gateway up')
    ping_min: Optional[int] = Field(default=1, ge=1, le=31,description='number of failure pings to declaring a gateway down')
    privacy: Optional[str] = Field(default='no', description='caller privacy on calls')


@librerouter.post("/libresbc/gateway", status_code=200)
def create_gateway(reqbody: GatewayModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        name_key = f'gateway:{name}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent gateway name'}; return
        rdbconn.hmset(name_key, redishash(data))
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_gateway, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/gateway/{identifier}", status_code=200)
def update_gateway(reqbody: GatewayModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        _name_key = f'gateway:{identifier}'
        name_key = f'gateway:{name}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent gateway identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent gateway name'}; return
        rdbconn.hmset(name_key, redishash(data))
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                weight = rdbconn.hget(f'intcon:out:{engagement}', identifier)
                pipe.hset(f'intcon:out:{engagement}', name, weight)
                pipe.hdel(f'intcon:out:{engagement}', identifier)
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_gateway, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/gateway/{identifier}", status_code=200)
def delete_gateway(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'gateway:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if rdbconn.scard(_engaged_key): 
            response.status_code, result = 403, {'error': 'engaged gateway'}; return
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent gateway identifier'}; return
        pipe.delete(_engaged_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_gateway, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/gateway/{identifier}", status_code=200)
def detail_gateway(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'gateway:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent gateway identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engaged_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_gateway, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/gateway", status_code=200)
def list_gateway(response: Response):
    result = None
    try:
        KEYPATTERN = f'gateway:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc', 'ip', 'port', 'transport')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            if detail:
                data.append({'name': getnameid(mainkey), 'desc': detail[0], 'ip': detail[1], 'port': detail[2], 'transport': detail[3]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_gateway, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------

def check_existent_codec(codec):
    if not rdbconn.exists(f'class:codec:{codec}'):
        raise ValueError('nonexistent class')
    return codec

def check_existent_capacity(capacity):
    if not rdbconn.exists(f'class:capacity:{capacity}'):
        raise ValueError('nonexistent class')
    return capacity

def check_existent_manipulation(manipulations):
    for manipulation in manipulations:
        if not rdbconn.exists(f'class:manipulation:{manipulation}'):
            raise ValueError('nonexistent class')
    return manipulations

def check_existent_translation(translations):
    for translation in translations:
        if not rdbconn.exists(f'class:translation:{translation}'):
            raise ValueError('nonexistent class')
    return translations

def check_existent_sipprofile(sipprofile):
    if not rdbconn.exists(f'sipprofile:{sipprofile}'):
        raise ValueError('nonexistent sipprofile')
    return sipprofile

def check_cluster_node(nodes):
    for node in nodes:
        if node != '_ALL_' and node not in CLUSTERMEMBERS:
            raise ValueError('nonexistent node')
    return nodes


#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# OUTBOUND INTERCONECTION
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------

class Distribution(str, Enum):
    round_robin = 'round_robin'
    hash_caller = 'hash_caller'
    hash_callee = 'hash_callee'
    hash_both = 'hash_both'
    hash_callid = 'hash_callid'
    weight_based = 'weight_based'


def check_existent_gateway(gateway):
    if not rdbconn.exists(f'gateway:{gateway}'):
        raise ValueError('nonexistent gateway')
    return gateway

class DistributedGatewayModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='gateway name')
    weight:  int = Field(default=1, ge=0, le=127, description='weight value use for distribution')
    # validation
    _existentgateway = validator('name')(check_existent_gateway)


class OutboundInterconnection(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of outbound interconnection')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    sipprofile: str = Field(description='a sip profile nameid that interconnection engage to')
    distribution: Distribution = Field(default='round_robin', description='The dispatcher algorithm to selects a destination from addresses set')
    gateways: List[DistributedGatewayModel] = Field(min_items=1, max_item=10, description='gateways list used for this interconnection')
    rtp_nets: List[IPv4Network] = Field(min_items=1, max_item=20, description='a set of IPv4 Network that use for RTP')
    codec_class: str = Field(description='nameid of codec class')
    capacity_class: str = Field(description='nameid of capacity class')
    translation_classes: List[str] = Field(default=[], min_items=0, max_item=5, description='a set of translation class')
    manipulation_classes: List[str] = Field(default=[], min_items=0, max_item=5, description='a set of manipulations class')
    nodes: List[str] = Field(default=['_ALL_'], min_items=1, max_item=len(CLUSTERMEMBERS), description='a set of node member that interconnection engage to')
    enable: bool = Field(default=True, description='enable/disable this interconnection')
    # validation
    _existentcodec = validator('codec_class', allow_reuse=True)(check_existent_codec)
    _existentcapacity = validator('capacity_class', allow_reuse=True)(check_existent_capacity)
    _existenttranslation = validator('translation_classes', allow_reuse=True)(check_existent_translation)
    _existentmanipulation = validator('manipulation_classes', allow_reuse=True)(check_existent_manipulation)
    _existentsipprofile = validator('sipprofile', allow_reuse=True)(check_existent_sipprofile)
    _clusternode = validator('nodes', allow_reuse=True)(check_cluster_node)

@librerouter.post("/libresbc/interconnection/outbound", status_code=200)
def create_outbound_interconnection(reqbody: OutboundInterconnection, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        sipprofile = data.get('sipprofile')
        gateways = {gw.get('name'):gw.get('weight') for gw in data.get('gateways')}
        rtp_nets = set(data.get('rtp_nets'))
        codec_class = data.get('codec_class')
        capacity_class = data.get('capacity_class')
        translation_classes = data.get('translation_classes')
        manipulation_classes = data.get('manipulation_classes')
        nodes = set(data.get('nodes'))
        # verification
        nameid = f'out:{name}'; name_key = f'intcon:{nameid}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent outbound interconnection'}; return
        # processing
        data.pop('gateways'); data.update({'rtp_nets': rtp_nets, 'nodes': nodes })
        pipe.hmset(name_key, redishash(data))
        pipe.sadd(f'engagement:sipprofile:{sipprofile}', nameid)
        for node in nodes: pipe.sadd(f'engagement:node:{node}', nameid)
        pipe.sadd(f'engagement:class:codec:{codec_class}', nameid)
        pipe.sadd(f'engagement:class:capacity:{capacity_class}', nameid)
        for translation in translation_classes: pipe.sadd(f'engagement:class:translation:{translation}', nameid)
        for manipulation in manipulation_classes: pipe.sadd(f'engagement:class:manipulation:{manipulation}', nameid)
        pipe.hmset(f'intcon:{nameid}:_gateways', redishash(gateways))
        for gateway in gateways: pipe.sadd(f'engagement:gateway:{gateway}', name)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_outbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/interconnection/outbound/{identifier}", status_code=200)
def update_outbound_interconnection(reqbody: OutboundInterconnection, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        sipprofile = data.get('sipprofile')
        gateways = {gw.get('name'):gw.get('weight') for gw in data.get('gateways')}
        rtp_nets = set(data.get('rtp_nets'))
        codec_class = data.get('codec_class')
        capacity_class = data.get('capacity_class')
        translation_classes = data.get('translation_classes')
        manipulation_classes = data.get('manipulation_classes')
        nodes = set(data.get('nodes'))
        # verification
        nameid = f'out:{name}'; name_key = f'intcon:{nameid}'
        _nameid = f'out:{identifier}'; _name_key = f'intcon:{_nameid}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent outbound interconnection identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent outbound interconnection name'}; return
        # get current data
        _data = jsonhash(rdbconn.hgetall(_name_key))
        _sipprofile = _data.get('sipprofile')
        _nodes = _data.get('nodes')
        _codec_class = _data.get('codec_class')
        _capacity_class = _data.get('capacity_class')
        _translation_classes = _data.get('translation_classes')
        _manipulation_classes = _data.get('manipulation_classes')
        _sip_ips = _data.get('sip_ips')
        _gateways = jsonhash(rdbconn.hgetall(f'intcon:{nameid}:_gateways'))
        # transaction block
        pipe.multi()
        # processing: removing old-one
        pipe.srem(f'engagement:sipprofile:{_sipprofile}', _nameid)
        for node in _nodes: pipe.srem(f'engagement:node:{node}', _nameid)
        pipe.srem(f'engagement:class:codec:{_codec_class}', _nameid)
        pipe.srem(f'engagement:class:capacity:{_capacity_class}', _nameid)
        for translation in _translation_classes: pipe.srem(f'engagement:class:translation:{translation}', _nameid)
        for manipulation in _manipulation_classes: pipe.srem(f'engagement:class:manipulation:{manipulation}', _nameid)
        for gateway in _gateways: pipe.srem(f'engagement:gateway:{gateway}', identifier)
        pipe.delete(f'intcon:{_nameid}:_gateways')
        # processing: adding new-one
        data.pop('gateways'); data.update({'rtp_nets': rtp_nets, 'nodes': nodes })
        pipe.hmset(name_key, redishash(data))
        pipe.sadd(f'engagement:sipprofile:{sipprofile}', nameid)
        for node in nodes: pipe.sadd(f'engagement:node:{node}', nameid)
        pipe.sadd(f'engagement:class:codec:{codec_class}', nameid)
        pipe.sadd(f'engagement:class:capacity:{capacity_class}', nameid)
        for translation in translation_classes: pipe.sadd(f'engagement:class:translation:{translation}', nameid)
        for manipulation in manipulation_classes: pipe.sadd(f'engagement:class:manipulation:{manipulation}', nameid)
        pipe.hmset(f'intcon:{nameid}:_gateways', redishash(gateways))
        for gateway in gateways: pipe.sadd(f'engagement:gateway:{gateway}', name)
        # change identifier
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                if engagement.startswith('table'):
                    pipe.hset(f'routing:{engagement}', 'endpoint', name)
                if engagement.startswith('record'):
                    endpoints = rdbconn.hget(f'routing:{engagement}', 'endpoints')
                    pipe.hset(f'routing:{engagement}', 'endpoints', f':list:{endpoints[6:].replace(identifier, name)}')
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
            pipe.execute()

        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_outbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/interconnection/outbound/{identifier}", status_code=200)
def delete_outbound_interconnection(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _nameid = f'out:{identifier}'; _name_key = f'intcon:{_nameid}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent outbound interconnection identifier'}; return
        if rdbconn.scard(_engaged_key): 
            response.status_code, result = 403, {'error': 'engaged outbound interconnection'}; return
        # get current data
        _data = jsonhash(rdbconn.hgetall(_name_key))
        _sipprofile = _data.get('sipprofile')
        _nodes = _data.get('nodes')
        _codec_class = _data.get('codec_class')
        _capacity_class = _data.get('capacity_class')
        _translation_classes = _data.get('translation_classes')
        _manipulation_classes = _data.get('manipulation_classes')
        _sip_ips = _data.get('sip_ips')
        _gateways = jsonhash(rdbconn.hgetall(f'intcon:{_nameid}:_gateways'))
        # processing: removing old-one
        pipe.srem(f'engagement:sipprofile:{_sipprofile}', _nameid)
        for node in _nodes: pipe.srem(f'engagement:node:{node}', _nameid)
        pipe.srem(f'engagement:class:codec:{_codec_class}', _nameid)
        pipe.srem(f'engagement:class:capacity:{_capacity_class}', _nameid)
        for translation in _translation_classes: pipe.srem(f'engagement:class:translation:{translation}', _nameid)
        for manipulation in _manipulation_classes: pipe.srem(f'engagement:class:manipulation:{manipulation}', _nameid)
        for gateway in _gateways: pipe.srem(f'engagement:gateway:{gateway}', identifier)
        pipe.delete(f'intcon:{_nameid}:_gateways')
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_outbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/interconnection/outbound/{identifier}", status_code=200)
def detail_outbound_interconnection(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _nameid = f'out:{identifier}'
        _name_key = f'intcon:{_nameid}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent outbound interconnection identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        gateways = [{'name': k, 'weigth': v} for k,v in jsonhash(rdbconn.hgetall(f'intcon:{_nameid}:_gateways')).items()]
        engagements = rdbconn.smembers(_engaged_key)
        result.update({'gateways': gateways, 'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_outbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
@librerouter.get("/libresbc/interconnection/outbound", status_code=200)
def list_outbound_interconnect(response: Response):
    result = None
    try:
        KEYPATTERN = 'intcon:out:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'name', 'desc', 'sipprofile')
        details = pipe.execute()

        data = list(); PYPATTERN = re.compile('^intcon:out:[^:]+$')
        for mainkey, detail in zip(mainkeys, details):
            if PYPATTERN.match(mainkey):
                data.append({'name': detail[0], 'desc': detail[1], 'sipprofile': detail[2]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_outbound_interconnect, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# INBOUND INTERCONECTION
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------

def check_existent_routing(table):
    if not rdbconn.exists(f'routing:table:{table}'):
        raise ValueError('nonexistent routing')
    return table

def check_existent_ringtone(ringtone):
    if not rdbconn.exists(f'class:ringtone:{ringtone}'):
        raise ValueError('nonexistent class')
    return ringtone

class InboundInterconnection(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of inbound interconnection')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    sipprofile: str = Field(description='a sip profile nameid that interconnection engage to')
    routing: str = Field(description='routing table that will be used by this inbound interconnection') 
    sip_ips: List[IPv4Address] = Field(min_items=1, max_item=10, description='a set of signalling that use for SIP')
    rtp_nets: List[IPv4Network] = Field(min_items=1, max_item=20, description='a set of IPv4 Network that use for RTP')
    ringready: bool = Field(default=False, description='response 180 ring indication')
    codec_class: str = Field(description='nameid of codec class')
    capacity_class: str = Field(description='nameid of capacity class')
    translation_classes: List[str] = Field(default=[], min_items=0, max_item=5, description='a set of translation class')
    manipulation_classes: List[str] = Field(default=[], min_items=0, max_item=5, description='a set of manipulations class')
    ringtone_class: str = Field(default=None, description='nameid of ringtone class')
    nodes: List[str] = Field(default=['_ALL_'], min_items=1, max_item=len(CLUSTERMEMBERS), description='a set of node member that interconnection engage to')
    enable: bool = Field(default=True, description='enable/disable this interconnection')
    # validation
    _existenringtone = validator('ringtone_class')(check_existent_ringtone)
    _existentcodec = validator('codec_class', allow_reuse=True)(check_existent_codec)
    _existentcapacity = validator('capacity_class', allow_reuse=True)(check_existent_capacity)
    _existenttranslation = validator('translation_classes', allow_reuse=True)(check_existent_translation)
    _existentmanipulation = validator('manipulation_classes', allow_reuse=True)(check_existent_translation)
    _existentsipprofile = validator('sipprofile', allow_reuse=True)(check_existent_sipprofile)
    _existentrouting = validator('routing')(check_existent_routing)
    _clusternode = validator('nodes', allow_reuse=True)(check_cluster_node)


@librerouter.post("/libresbc/interconnection/inbound", status_code=200)
def create_inbound_interconnection(reqbody: InboundInterconnection, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        sipprofile = data.get('sipprofile')
        routing = data.get('routing')
        sip_ips = set(data.get('sip_ips'))
        rtp_nets = set(data.get('rtp_nets'))
        codec_class = data.get('codec_class')
        ringtone_class = data.get('ringtone_class')
        capacity_class = data.get('capacity_class')
        translation_classes = data.get('translation_classes')
        manipulation_classes = data.get('manipulation_classes')
        nodes = set(data.get('nodes'))
        # verification
        nameid = f'in:{name}'; name_key = f'intcon:{nameid}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent inbound interconnection'}; return
        for sip_ip in sip_ips:
            if rdbconn.exists(f'recognition:{sipprofile}:{sip_ip}'):
                response.status_code, result = 403, {'error': 'existent sip ip'}; return
        # processing
        data.update({'sip_ips': sip_ips, 'rtp_nets': rtp_nets, 'nodes': nodes })
        pipe.hmset(name_key, redishash(data))
        pipe.sadd(f'engagement:sipprofile:{sipprofile}', nameid)
        pipe.sadd(f'engagement:routing:{routing}', nameid)
        for node in nodes: pipe.sadd(f'engagement:node:{node}', nameid)
        pipe.sadd(f'engagement:class:codec:{codec_class}', nameid)
        pipe.sadd(f'engagement:class:ringtone:{ringtone_class}', nameid)
        pipe.sadd(f'engagement:class:capacity:{capacity_class}', nameid)
        for translation in translation_classes: pipe.sadd(f'engagement:class:translation:{translation}', nameid)
        for manipulation in manipulation_classes: pipe.sadd(f'engagement:class:manipulation:{manipulation}', nameid)
        for sip_ip in sip_ips: pipe.set(f'recognition:{sipprofile}:{sip_ip}', name)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_inbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.put("/libresbc/interconnection/inbound/{identifier}", status_code=200)
def update_inbound_interconnection(reqbody: InboundInterconnection, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        sipprofile = data.get('sipprofile')
        sip_ips = set(data.get('sip_ips'))
        rtp_nets = set(data.get('rtp_nets'))
        routing = data.get('routing')
        codec_class = data.get('codec_class')
        ringtone_class = data.get('ringtone_class')
        capacity_class = data.get('capacity_class')
        translation_classes = data.get('translation_classes')
        manipulation_classes = data.get('manipulation_classes')
        nodes = set(data.get('nodes'))
        # verification
        nameid = f'in:{name}'; name_key = f'intcon:{nameid}'
        _nameid = f'in:{identifier}'; _name_key = f'intcon:{_nameid}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent inbound interconnection identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent inbound interconnection name'}; return
        for sip_ip in sip_ips:
            _name = rdbconn.exists(f'recognition:{sipprofile}:{sip_ip}')
            if _name and _name != name:
                response.status_code, result = 403, {'error': 'existent sip ip'}; return
        # get current data
        _data = jsonhash(rdbconn.hgetall(_name_key))
        _sipprofile = _data.get('sipprofile')
        _routing = _data.get('routing')
        _nodes = set(_data.get('nodes'))
        _codec_class = _data.get('codec_class')
        _ringtone_class = _data.get('ringtone_class')
        _capacity_class = _data.get('capacity_class')
        _translation_classes = _data.get('translation_classes')
        _manipulation_classes = _data.get('manipulation_classes')
        _sip_ips = set(_data.get('sip_ips'))
        # transaction block
        pipe.multi()
        # processing: removing old-one
        pipe.srem(f'engagement:sipprofile:{_sipprofile}', _nameid)
        pipe.srem(f'engagement:routing:{_routing}', _nameid)
        for node in _nodes: pipe.srem(f'engagement:node:{node}', _nameid)
        pipe.srem(f'engagement:class:codec:{_codec_class}', _nameid)
        pipe.srem(f'engagement:class:ringtone:{_ringtone_class}', _nameid)
        pipe.srem(f'engagement:class:capacity:{_capacity_class}', _nameid)
        for translation in _translation_classes: pipe.srem(f'engagement:class:translation:{translation}', _nameid)
        for manipulation in _manipulation_classes: pipe.srem(f'engagement:class:manipulation:{manipulation}', _nameid)
        for sip_ip in _sip_ips: pipe.delete(f'recognition:{_sipprofile}:{sip_ip}') 
        # processing: adding new-one
        data.update({'sip_ips': sip_ips, 'rtp_nets': rtp_nets, 'nodes': nodes })
        pipe.hmset(name_key, redishash(data))
        pipe.sadd(f'engagement:sipprofile:{sipprofile}', nameid)
        pipe.sadd(f'engagement:routing:{routing}', nameid)
        for node in nodes: pipe.sadd(f'engagement:node:{node}', nameid)
        pipe.sadd(f'engagement:class:codec:{codec_class}', nameid)
        pipe.sadd(f'engagement:class:ringtone:{ringtone_class}', nameid)
        pipe.sadd(f'engagement:class:capacity:{capacity_class}', nameid)
        for translation in translation_classes: pipe.sadd(f'engagement:class:translation:{translation}', nameid)
        for manipulation in manipulation_classes: pipe.sadd(f'engagement:class:manipulation:{manipulation}', nameid)
        for sip_ip in sip_ips: pipe.set(f'recognition:{sipprofile}:{sip_ip}', name)   
        # change identifier
        if name != identifier:
            pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_inbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.delete("/libresbc/interconnection/inbound/{identifier}", status_code=200)
def delete_inbound_interconnection(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _nameid = f'in:{identifier}'; _name_key = f'intcon:{_nameid}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent inbound interconnection'}; return

        _data = jsonhash(rdbconn.hgetall(_name_key))
        _sipprofile = _data.get('sipprofile')
        _nodes = _data.get('nodes')
        _codec_class = _data.get('codec_class')
        _ringtone_class = _data.get('ringtone_class')
        _capacity_class = _data.get('capacity_class')
        _translation_classes = _data.get('translation_classes')
        _manipulation_classes = _data.get('manipulation_classes')
        _sip_ips = _data.get('sip_ips')

        pipe.srem(f'engagement:sipprofile:{_sipprofile}', _nameid)
        for node in _nodes: pipe.srem(f'engagement:node:{node}', _nameid)
        pipe.srem(f'engagement:class:codec:{_codec_class}', _nameid)
        pipe.srem(f'engagement:class:ringtone:{_ringtone_class}', _nameid)
        pipe.srem(f'engagement:class:capacity:{_capacity_class}', _nameid)
        for translation in _translation_classes: pipe.srem(f'engagement:class:translation:{translation}', _nameid)
        for manipulation in _manipulation_classes: pipe.srem(f'engagement:class:manipulation:{manipulation}', _nameid)
        for sip_ip in _sip_ips: pipe.delete(f'recognition:{_sipprofile}:{sip_ip}')  
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_inbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.get("/libresbc/interconnection/inbound/{identifier}", status_code=200)
def detail_inbound_interconnection(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'intcon:in:{identifier}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent inbound interconnection identifier'}; return
        result = rdbconn.hgetall(_name_key)
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_inbound_interconnection, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.get("/libresbc/interconnection/inbound", status_code=200)
def list_inbound_interconnect(response: Response):
    result = None
    try:
        KEYPATTERN = 'intcon:in:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys:
            pipe.hmget(mainkey, 'desc', 'sipprofile', 'routing')
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            data.append({'name': getnameid(mainkey), 'desc': detail[0], 'sipprofile': detail[1], 'routing': detail[2]})

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_inbound_interconnect, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUTING TABLE
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
_QUERY = 'query'
_BLOCK = 'block'
_JUMPS = 'jumps'
_ROUTE = 'route'
# reserved for value empty string
__EMPTY_STRING__ = '__empty_string__'

class RoutingVariableEnum(str, Enum):
    DONTCARE = 'DONTCARE'
    destination_number = 'destination_number'
    caller_id = 'caller_id'
    auth_user = 'auth_user'
    from_user = 'from_user'
    to_user = 'to_user'
    contact_user = 'contact_user'

class RoutingTableActionEnum(str, Enum):
    query = _QUERY
    route = _ROUTE
    block = _BLOCK
    # request: reseved routing with http api 

class RoutingTableModel(BaseModel):
    name: str = Field(regex=_NAME_, max_length=32, description='name of routing table')
    desc: Optional[str] = Field(default='', max_length=64, description='description')
    variables: List[RoutingVariableEnum] = Field(min_items=1, max_items=1, description='sip variable for routing base')
    action: RoutingTableActionEnum = Field(default='query', description=f'routing action, <{_QUERY}>: find nexthop by query routing record; <{_BLOCK}>: block the call; <{_ROUTE}>: route call to outbound interconnection')
    endpoint: Optional[str] = Field(description='designated endpoint for action')
    # validation
    @root_validator(pre=True)
    def routing_table_agreement(cls, values):
        action = values.get('action')
        endpoint = values.get('endpoint')
        if action==_ROUTE:
            if not endpoint:
                raise ValueError('endpoint must be provided for route action')
            else:
                if not rdbconn.exists(f'intcon:out:{endpoint}'):
                    raise ValueError('nonexistent outbound interconnection')
        else:
            if 'endpoint' in values: values.pop('endpoint')
        return values


@librerouter.post("/libresbc/routing/table", status_code=200)
def create_routing_table(reqbody: RoutingTableModel, response: Response):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        endpoint = data.get('endpoint')
        nameid = f'table:{name}'; name_key = f'routing:{nameid}'
        if rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent routing table'}; return
        pipe.hmset(name_key, redishash(data))
        if endpoint: 
            pipe.sadd(f'engagement:intcon:out:{endpoint}', nameid)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_routing_table, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.put("/libresbc/routing/table/{identifier}", status_code=200)
def update_routing_table(reqbody: RoutingTableModel, response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        name = reqbody.name
        data = jsonable_encoder(reqbody)
        endpoint = data.get('endpoint')
        _nameid = f'table:{identifier}'; _name_key = f'routing:{_nameid}'
        nameid = f'table:{name}'; name_key = f'routing:{nameid}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent routing table identifier'}; return
        if name != identifier and rdbconn.exists(name_key):
            response.status_code, result = 403, {'error': 'existent routing table name'}; return
        # get current data
        _endpoint = rdbconn.hget(_name_key, 'endpoint')
        # transaction block
        pipe.multi()
        if _endpoint: pipe.srem(f'engagement:intcon:out:{_endpoint}', _nameid)
        pipe.hmset(name_key, redishash(data))
        if endpoint: pipe.sadd(f'engagement:intcon:out:{endpoint}', nameid)
        if name != identifier:
            _engaged_key = f'engagement:{_name_key}'
            engaged_key = f'engagement:{name_key}'
            engagements = rdbconn.smembers(_engaged_key)
            for engagement in engagements:
                pipe.hset(f'routing:{engagement}', 'endpoints', f':list:{name}')
            if rdbconn.exists(_engaged_key):
                pipe.rename(_engaged_key, engaged_key)
            pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_routing_table, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.delete("/libresbc/routing/table/{identifier}", status_code=200)
def delete_routing_table(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _nameid = f'table:{identifier}'; _name_key = f'routing:{_nameid}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key):
            response.status_code, result = 403, {'error': 'nonexistent routing table'}; return
        if rdbconn.scard(_engaged_key): 
            response.status_code, result = 403, {'error': 'engaged routing table'}; return
        # check if routing records exists in table
        _ROUTING_KEY_PATTERN = f'routing:record:{identifier}:*'
        next, records = rdbconn.scan(0, _ROUTING_KEY_PATTERN, SCAN_COUNT)
        if records:
            response.status_code, result = 400, {'error': 'routing table in used'}; return
        else:
            while next:
                next, records = rdbconn.scan(next, _ROUTING_KEY_PATTERN, SCAN_COUNT)
                if records:
                    response.status_code, result = 400, {'error': 'routing table in used'}; return
        # get current data
        _endpoint = rdbconn.hget(_name_key, 'endpoint')
        if _endpoint: pipe.srem(f'engagement:intcon:out:{_endpoint}', _nameid)
        pipe.delete(_engaged_key)
        pipe.delete(_name_key)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_routing_table, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/routing/table/{identifier}", status_code=200)
def detail_routing_table(response: Response, identifier: str=Path(..., regex=_NAME_)):
    result = None
    try:
        _name_key = f'routing:table:{identifier}'
        _engaged_key = f'engagement:{_name_key}'
        if not rdbconn.exists(_name_key): 
            response.status_code, result = 403, {'error': 'nonexistent routing table identifier'}; return
        result = jsonhash(rdbconn.hgetall(_name_key))
        engagements = rdbconn.smembers(_engaged_key)
        result.update({'engagements': engagements})
        response.status_code = 200
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=detail_routing_table, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/routing/table", status_code=200)
def list_routing_table(response: Response):
    result = None
    try:
        KEYPATTERN = f'routing:table:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys: pipe.hgetall(mainkey)
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            data.append(jsonhash(detail))

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_routing_table, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUTING RECORD
#-----------------------------------------------------------------------------------------------------------------------------------------------------------------------

class MatchingEnum(str, Enum):
    lpm = 'lpm'
    em = 'em'

class RoutingRecordActionEnum(str, Enum): 
    route = 'route'
    block = 'block'
    jumps = 'jumps'

class RoutingRecordModel(BaseModel):
    table: str = Field(regex=_NAME_, max_length=32, description='name of routing table')
    match: MatchingEnum = Field(description='matching options, include lpm: longest prefix match, em: exact match')
    value: str = Field(max_length=128, description='value of variable that declared in routing table')
    action: RoutingRecordActionEnum = Field(default=_ROUTE, description=f'routing action, <{_JUMPS}>: jumps to other routing table; <{_BLOCK}>: block the call; <{_ROUTE}>: route call to outbound interconnection')
    endpoints: List[str] = Field(max_items=2, description='designated endpoint for action')
    load: Optional[int] = Field(default=100, ge=0, le=100, description='call load percentage over total 100, that apply for endpoints')
    # validation
    @root_validator(pre=True)
    def routing_record_agreement(cls, values):
        table = values.get('table')
        action = values.get('action')
        endpoints = values.get('endpoints')
        load = values.get('load')

        if not rdbconn.exists(f'routing:table:{table}'):
            raise ValueError('nonexistent routing table')
        
        if action==_BLOCK: 
            values.update({'endpoints': []})
            values.pop('load')
        if action==_JUMPS: 
            if len(endpoints)<1:
                raise ValueError(f'{_JUMPS} action require at least 1 routing table in endpoints')
            else:
                values.update({'endpoints': endpoints[0]})
                values.pop('load')
            # check endpoint of _JUMP
            endpoint = endpoints[0]
            if not rdbconn.exists(f'routing:table:{endpoint}'): 
                raise ValueError('nonexistent routing table in first endpoint')
        if action==_ROUTE:
            if len(endpoints)!=2:
                raise ValueError(f'{_ROUTE} action require 2 outbound interconnections in endpoints')
            if load==None:
                raise ValueError(f'{_ROUTE} action require load param')
            # check endpoint of _ROUTE
            for endpoint in endpoints:
                if not rdbconn.exists(f'intcon:out:{endpoint}'):
                    raise ValueError('nonexistent outbound interconnect')
        return values


@librerouter.post("/libresbc/routing/record", status_code=200)
def create_routing_record(reqbody: RoutingRecordModel, response: Response):
    result = None
    try:
        data = jsonable_encoder(reqbody)
        table = data.get('table')
        match = data.get('match')
        value = data.get('value')
        action = data.get('action')
        endpoints = data.get('endpoints')

        nameid = f'record:{table}:{match}:{value}'; record_key = f'routing:{nameid}'
        if rdbconn.exists(record_key):
            response.status_code, result = 403, {'error': 'existent routing record'}; return
        
        data.pop('table'); data.pop('match'); data.pop('value')
        pipe.hmset(record_key, redishash(data))
        if action==_ROUTE:
            for endpoint in endpoints:
                pipe.sadd(f'engagement:intcon:out:{endpoint}', nameid)
        if action==_JUMPS:
            for endpoint in endpoints:
                pipe.sadd(f'engagement:routing:table:{endpoint}', nameid)

        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=create_routing_record, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.put("/libresbc/routing/record", status_code=200)
def update_routing_record(reqbody: RoutingRecordModel, response: Response):
    result = None
    try:
        data = jsonable_encoder(reqbody)
        table = data.get('table')
        match = data.get('match')
        value = data.get('value')
        action = data.get('action')
        endpoints = data.get('endpoints')

        nameid = f'record:{table}:{match}:{value}'; record_key = f'routing:{nameid}'
        if not rdbconn.exists(record_key):
            response.status_code, result = 403, {'error': 'non existent routing record'}; return
        # get current data
        _data = jsonhash(rdbconn.hgetall(record_key))
        _action = _data.get('action')
        _endpoints = _data.get('endpoints')
        # update new-one
        data.pop('table'); data.pop('match'); data.pop('value')
        pipe.hmset(record_key, redishash(data))
        if action==_ROUTE:
            for endpoint in endpoints:
                pipe.sadd(f'engagement:intcon:out:{endpoint}', nameid)
        if action==_JUMPS:
            for endpoint in endpoints:
                pipe.sadd(f'engagement:routing:table:{endpoint}', nameid)
        # remove new-one
        if _action==_ROUTE:
            for endpoint in _endpoints:
                pipe.srem(f'engagement:intcon:out:{endpoint}', nameid)
        if _action==_JUMPS:
            for endpoint in _endpoints:
                pipe.srem(f'engagement:routing:table:{endpoint}', nameid)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=update_routing_record, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result


@librerouter.delete("/libresbc/routing/record/{table}/{match}/{value}", status_code=200)
def delete_routing_record(response: Response, value:str, table:str=Path(..., regex=_NAME_), match:str=Path(..., regex='^(em|lpm)$')):
    result = None
    try:
        if value == __EMPTY_STRING__: value = ''
        nameid = f'record:{table}:{match}:{value}'; record_key = f'routing:{nameid}'
        if not rdbconn.exists(record_key):
            response.status_code, result = 403, {'error': 'notexistent routing record'}; return

        _data = jsonhash(rdbconn.hgetall(record_key))
        _action = _data.get('action')
        _endpoints = _data.get('endpoints')

        pipe.delete(record_key)
        if _action==_ROUTE:
            for endpoint in _endpoints:
                pipe.srem(f'engagement:intcon:out:{endpoint}', nameid)
        if _action==_JUMPS:
            for endpoint in _endpoints:
                pipe.srem(f'engagement:routing:table:{endpoint}', nameid)
        pipe.execute()
        response.status_code, result = 200, {'passed': True}
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=delete_routing_record, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result

@librerouter.get("/libresbc/routing/record/{table}", status_code=200)
def list_routing_record(response: Response, table:str=Path(..., regex=_NAME_)):
    result = None
    try:
        if not rdbconn.exists(f'routing:table:{table}'): 
            response.status_code, result = 403, {'error': 'nonexistent routing table identifier'}; return

        KEYPATTERN = f'routing:record:{table}:*'
        next, mainkeys = rdbconn.scan(0, KEYPATTERN, SCAN_COUNT)
        while next:
            next, tmpkeys = rdbconn.scan(next, KEYPATTERN, SCAN_COUNT)
            mainkeys += tmpkeys

        for mainkey in mainkeys: pipe.hgetall(mainkey)
        details = pipe.execute()

        data = list()
        for mainkey, detail in zip(mainkeys, details):
            records = listify(mainkey)
            detail.update({'match': records[-2], 'value': records[-1]})
            data.append(jsonhash(detail))

        response.status_code, result = 200, data
    except Exception as e:
        response.status_code, result = 500, None
        logify(f"module=liberator, space=libreapi, action=list_routing_record, requestid={get_request_uuid()}, exception={e}, traceback={traceback.format_exc()}")
    finally:
        return result