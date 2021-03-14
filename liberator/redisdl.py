try:
    import json
except ImportError:
    import simplejson as json
import redis
import sys
import time as _time
import functools

have_streaming_load = have_ijson = have_jsaone = False
try:
    import ijson as ijson_mod
    have_streaming_load = True
    have_ijson = True
    default_streaming_backend = 'ijson'
except ImportError:
    try:
        import jsaone as jsaone_mod
        have_streaming_load = True
        have_jsaone = True
        default_streaming_backend = 'jsaone'
    except ImportError:
        pass

py3 = sys.version_info[0] == 3

if py3:
    base_exception_class = Exception
else:
    base_exception_class = StandardError

class UnknownTypeError(base_exception_class):
    pass

class ConcurrentModificationError(base_exception_class):
    pass

# internal exceptions

class KeyDeletedError(base_exception_class):
    pass

class KeyTypeChangedError(base_exception_class):
    pass

class RedisWrapper(redis.Redis):
    def __init__(self, *args, **kwargs):
        super(RedisWrapper, self).__init__(*args, **kwargs)

        version = [int(part) for part in self.info()['redis_version'].split('.')]
        self.have_pttl = version >= [2, 6]

    def pttl_or_ttl(self, key):
        if self.have_pttl:
            pttl = self.pttl(key)
            if pttl is None or pttl == -1:
                return None
            else:
                return float(pttl) / 1000
        else:
            ttl = self.ttl(key)
            if ttl is None or ttl == -1:
                return None
            else:
                return ttl

    def pttl_or_ttl_pipeline(self, p, key):
        if self.have_pttl:
            return p.pttl(key)
        else:
            return p.ttl(key)

    def decode_pttl_or_ttl_pipeline_value(self, value):
        if value is None or value == -1:
            return None
        if self.have_pttl:
            return float(value) / 1000
        else:
            return value

    def pexpire_or_expire(self, key, ttl):
        if self.have_pttl:
            return self.pexpire(key, int(ttl * 1000))
        else:
            # rounds the ttl down always
            return self.expire(key, int(ttl))

    def pexpireat_or_expireat(self, key, time):
        if self.have_pttl:
            return self.pexpireat(key, int(time * 1000))
        else:
            # rounds the expiration time down always
            return self.expireat(key, int(time))

    def pexpire_or_expire_pipeline(self, p, key, ttl):
        if self.have_pttl:
            return p.pexpire(key, int(ttl * 1000))
        else:
            # rounds the ttl down always
            return p.expire(key, int(ttl))

    def pexpireat_or_expireat_pipeline(self, p, key, time):
        if self.have_pttl:
            return p.pexpireat(key, int(time * 1000))
        else:
            # rounds the expiration time down always
            return p.expireat(key, int(time))

def client(host='localhost', port=6379, password=None, db=0,
                 unix_socket_path=None, encoding='utf-8'):
    if unix_socket_path is not None:
        r = RedisWrapper(unix_socket_path=unix_socket_path,
                        password=password,
                        db=db,
                        charset=encoding)
    else:
        r = RedisWrapper(host=host,
                        port=port,
                        password=password,
                        db=db,
                        charset=encoding)
    return r

def dumps(host='localhost', port=6379, password=None, db=0, pretty=False,
          unix_socket_path=None, encoding='utf-8', keys='*'):
    r = client(host=host, port=port, password=password, db=db,
               unix_socket_path=unix_socket_path, encoding=encoding)
    kwargs = {}
    if not pretty:
        kwargs['separators'] = (',', ':')
    else:
        kwargs['indent'] = 2
        kwargs['sort_keys'] = True
    encoder = json.JSONEncoder(**kwargs)
    table = {}
    for key, type, ttl, value in _reader(r, pretty, encoding, keys):
        table[key] = subd = {'type': type, 'value': value}
        if ttl is not None:
            subd['ttl'] = ttl
            subd['expireat'] = _time.time() + ttl
    return encoder.encode(table)

class BytesWriteWrapper(object):
    def __init__(self, stream):
        self.stream = stream
        
    def write(self, str):
        return self.stream.write(str.encode())

def dump(fp, host='localhost', port=6379, password=None, db=0, pretty=False,
         unix_socket_path=None, encoding='utf-8', keys='*'):
    
    try:
        fp.write('')
    except TypeError:
        fp = BytesWriteWrapper(fp)
    
    if pretty:
        # hack to avoid implementing pretty printing
        fp.write(dumps(host=host, port=port, password=password, db=db,
            pretty=pretty, encoding=encoding, keys=keys))
        return

    r = client(host=host, port=port, password=password, db=db,
               unix_socket_path=unix_socket_path, encoding=encoding)
    kwargs = {}
    if not pretty:
        kwargs['separators'] = (',', ':')
    else:
        kwargs['indent'] = 2
        kwargs['sort_keys'] = True
    encoder = json.JSONEncoder(**kwargs)
    fp.write('{')
    first = True
    for key, type, ttl, value in _reader(r, pretty, encoding, keys):
        key = encoder.encode(key)
        type = encoder.encode(type)
        value = encoder.encode(value)
        if ttl:
            expireat = encoder.encode(_time.time() + ttl)
            ttl = encoder.encode(ttl)
            item = '%s:{"type":%s,"value":%s,"ttl":%s,"expireat":%s}' % (
                key, type, value, ttl, expireat)
        else:
            item = '%s:{"type":%s,"value":%s}' % (key, type, value)
        if first:
            first = False
        else:
            fp.write(',')
        fp.write(item)
    fp.write('}')

class StringReader(object):
    @staticmethod
    def send_command(p, key):
        p.get(key)

    @staticmethod
    def handle_response(response, pretty, encoding):
        # if key does not exist, get will return None;
        # however, our type check requires that the key exists
        return response.decode(encoding)

class ListReader(object):
    @staticmethod
    def send_command(p, key):
        p.lrange(key, 0, -1)

    @staticmethod
    def handle_response(response, pretty, encoding):
        return [v.decode(encoding) for v in response]

class SetReader(object):
    @staticmethod
    def send_command(p, key):
        p.smembers(key)

    @staticmethod
    def handle_response(response, pretty, encoding):
        value = [v.decode(encoding) for v in response]
        if pretty:
            value.sort()
        return value

class ZsetReader(object):
    @staticmethod
    def send_command(p, key):
        p.zrange(key, 0, -1, False, True)

    @staticmethod
    def handle_response(response, pretty, encoding):
        return [(k.decode(encoding), score) for k, score in response]

class HashReader(object):
    @staticmethod
    def send_command(p, key):
        p.hgetall(key)

    @staticmethod
    def handle_response(response, pretty, encoding):
        value = {}
        for k in response:
            value[k.decode(encoding)] = response[k].decode(encoding)
        return value

readers = {
    'string': StringReader,
    'list': ListReader,
    'set': SetReader,
    'zset': ZsetReader,
    'hash': HashReader,
}

# note: key is a byte string
def _read_key(key, r, pretty, encoding):
    type = r.type(key).decode('ascii')
    if type == 'none':
        # key was deleted by a concurrent operation on the data store
        raise KeyDeletedError
    reader = readers.get(type)
    if reader is None:
        raise UnknownTypeError("Unknown key type: %s" % type)
    p = r.pipeline()
    p.watch(key)
    p.multi()
    p.type(key)
    r.pttl_or_ttl_pipeline(p, key)
    reader.send_command(p, key)
    # might raise redis.WatchError
    results = p.execute()
    actual_type = results[0].decode('ascii')
    if actual_type != type:
        # type changed, retry
        raise KeyTypeChangedError

    ttl = r.decode_pttl_or_ttl_pipeline_value(results[1])
    value = reader.handle_response(results[2], pretty, encoding)
    return (type, ttl, value)

def _reader(r, pretty, encoding, keys='*'):
    for encoded_key in r.keys(keys):
        key = encoded_key.decode(encoding)
        handled = False
        for i in range(10):
            try:
                type, ttl, value = _read_key(encoded_key, r, pretty, encoding)
                yield key, type, ttl, value
                handled = True
                break
            except KeyDeletedError:
                # do not dump the key
                handled = True
                break
            except redis.WatchError:
                # same logic as key type changed
                pass
            except KeyTypeChangedError:
                # retry reading type again
                pass
        if not handled:
            # ran out of retries
            raise ConcurrentModificationError('Key %s is being concurrently modified' % key)

def _empty(r):
    for key in r.keys():
        r.delete(key)

def loads(s, host='localhost', port=6379, password=None, db=0, empty=False,
          unix_socket_path=None, encoding='utf-8', use_expireat=False):
    r = client(host=host, port=port, password=password, db=db,
               unix_socket_path=unix_socket_path, encoding=encoding)
    if empty:
        _empty(r)
    table = json.loads(s)
    counter = 0
    for key in table:
        # Create pipeline:
        if not counter:
            p = r.pipeline(transaction=False)
        item = table[key]
        type = item['type']
        value = item['value']
        ttl = item.get('ttl')
        expireat = item.get('expireat')
        _writer(r, p, key, type, value, ttl, expireat, use_expireat=use_expireat)
        # Increase counter until 10 000...
        counter = (counter + 1) % 10000
        # ... then execute:
        if not counter:
            p.execute()
    if counter:
        # Finally, execute again:
        p.execute()

def load_lump(fp, host='localhost', port=6379, password=None, db=0,
    empty=False, unix_socket_path=None, encoding='utf-8', use_expireat=False,
):
    s = fp.read()
    if py3:
        # s can be a string or a bytes instance.
        # if bytes, decode to a string because loads requires input to be a string.
        if isinstance(s, bytes):
            s = s.decode(encoding)
    loads(s, host, port, password, db, empty, unix_socket_path, encoding, use_expireat=use_expireat)

def get_ijson(local_streaming_backend):
    if local_streaming_backend:
        __import__('ijson.backends.%s' % local_streaming_backend)
        ijson = getattr(ijson_mod.backends, local_streaming_backend)
    else:
        ijson = ijson_mod
    return ijson

def ijson_top_level_items(file, local_streaming_backend):
    ijson = get_ijson(local_streaming_backend)
    parser = ijson.parse(file)
    prefixed_events = iter(parser)
    wanted = None
    try:
        while True:
            current, event, value = next(prefixed_events)
            if current != '':
                wanted = current
                if event in ('start_map', 'start_array'):
                    builder = ijson_mod.ObjectBuilder()
                    end_event = event.replace('start', 'end')
                    while (current, event) != (wanted, end_event):
                        builder.event(event, value)
                        current, event, value = next(prefixed_events)
                    yield current, builder.value
    except StopIteration:
        pass

class TextReadWrapper(object):
    def __init__(self, fp):
        self.fp = fp
        
    def read(self, *args, **kwargs):
        return self.fp.read(*args, **kwargs).decode()

class BytesReadWrapper(object):
    def __init__(self, fp):
        self.fp = fp
        
    def read(self, *args, **kwargs):
        return self.fp.read(*args, **kwargs).encode('utf-8')

def create_loader(fp, streaming_backend=None):
    if not have_streaming_load:
        raise TypeError('Cannot create a streaming loader - neither ijson nor jsaone are present')

    if streaming_backend is None:
        streaming_backend = default_streaming_backend
    if '-' in streaming_backend:
        lib, option = streaming_backend.split('-')
        if lib not in ('ijson', 'jsaone'):
            raise TypeError('Invalid streaming backend requested: %s' % streaming_backend)
    elif streaming_backend == 'ijson':
        lib = 'ijson'
        option = None
    elif streaming_backend == 'jsaone':
        lib = 'jsaone'
        option = None
    else:
        lib = 'ijson'
        option = streaming_backend

    if lib == 'ijson':
        if not have_ijson:
            raise TypeError('%s backend requested but ijson is not present' % streaming_backend)
        if py3 and isinstance(fp.read(0), str):
            fp = BytesReadWrapper(fp)
        def loader():
            return ijson_top_level_items(fp, option)
    else:
        if not have_jsaone:
            raise TypeError('jsaone backend requested but jsaone is not present')
        if py3 and isinstance(fp.read(0), bytes):
            # jsaone can only process text string data (str), not bytes
            fp = TextReadWrapper(fp)
        def loader():
            return jsaone_mod.load(fp)

    return loader

def load_streaming(fp, host='localhost', port=6379, password=None, db=0,
    empty=False, unix_socket_path=None, encoding='utf-8', use_expireat=False,
    streaming_backend=None,
):
    loader = create_loader(fp, streaming_backend)

    r = client(host=host, port=port, password=password, db=db,
               unix_socket_path=unix_socket_path, encoding=encoding)

    counter = 0
    for key, item in loader():
        # Create pipeline:
        if not counter:
            p = r.pipeline(transaction=False)
        type = item['type']
        value = item['value']
        ttl = item.get('ttl')
        expireat = item.get('expireat')
        _writer(r, p, key, type, value, ttl, expireat, use_expireat=use_expireat)
        # Increase counter until 10 000...
        counter = (counter + 1) % 10000
        # ... then execute:
        if not counter:
            p.execute()
    if counter:
        # Finally, execute again:
        p.execute()

def load(fp, host='localhost', port=6379, password=None, db=0,
    empty=False, unix_socket_path=None, encoding='utf-8', use_expireat=False,
    streaming_backend=None,
):
    if have_streaming_load:
        load_streaming(fp, host=host, port=port, password=password, db=db,
            empty=empty, unix_socket_path=unix_socket_path, encoding=encoding,
            use_expireat=use_expireat, streaming_backend=streaming_backend)
    else:
        load_lump(fp, host=host, port=port, password=password, db=db,
            empty=empty, unix_socket_path=unix_socket_path, encoding=encoding,
            use_expireat=use_expireat)

def _writer(r, p, key, type, value, ttl, expireat, use_expireat):
    p.delete(key)
    if type == 'string':
        p.set(key, value)
    elif type == 'list':
        for element in value:
            p.rpush(key, element)
    elif type == 'set':
        for element in value:
            p.sadd(key, element)
    elif type == 'zset':
        for element, score in value:
            p.zadd(key, element, score)
    elif type == 'hash':
        p.hmset(key, value)
    else:
        raise UnknownTypeError("Unknown key type: %s" % type)

    if use_expireat:
        if expireat is not None:
            r.pexpireat_or_expireat_pipeline(p, key, expireat)
        elif ttl is not None:
            r.pexpire_or_expire_pipeline(p, key, ttl)
    else:
        if ttl is not None:
            r.pexpire_or_expire_pipeline(p, key, ttl)
        elif expireat is not None:
            r.pexpireat_or_expireat_pipeline(p, key, expireat)

def main():
    import optparse
    import os.path
    import re
    import sys

    DUMP = 1
    LOAD = 2

    def options_to_kwargs(options):
        args = {}
        if options.host:
            args['host'] = options.host
        if options.port:
            args['port'] = int(options.port)
        if options.socket:
            args['unix_socket_path'] = options.socket
        if options.password:
            args['password'] = options.password
        if options.db:
            args['db'] = int(options.db)
        if options.encoding:
            args['encoding'] = options.encoding
        # dump only
        if hasattr(options, 'pretty') and options.pretty:
            args['pretty'] = True
        if hasattr(options, 'keys') and options.keys:
            args['keys'] = options.keys
        # load only
        if hasattr(options, 'use_expireat') and options.use_expireat:
            args['use_expireat'] = True
        if hasattr(options, 'empty') and options.empty:
            args['empty'] = True
        if hasattr(options, 'backend') and options.backend:
            args['streaming_backend'] = options.backend
        return args

    def do_dump(options):
        if options.output:
            output = open(options.output, 'w')
        else:
            output = sys.stdout

        kwargs = options_to_kwargs(options)
        dump(output, **kwargs)

        if options.output:
            output.close()

    def do_load(options, args):
        if len(args) > 0:
            input = open(args[0], 'rb')
        else:
            input = sys.stdin

        kwargs = options_to_kwargs(options)
        load(input, **kwargs)

        if len(args) > 0:
            input.close()

    script_name = os.path.basename(sys.argv[0])
    if re.search(r'load(?:$|\.)', script_name):
        action = help = LOAD
    elif re.search(r'dump(?:$|\.)', script_name):
        action = help = DUMP
    else:
        # default is dump, however if dump is specifically requested
        # we don't show help text for toggling between dumping and loading
        action = DUMP
        help = None

    if help == LOAD:
        usage = "Usage: %prog [options] [FILE]"
        usage += "\n\nLoad data from FILE (which must be a JSON dump previously created"
        usage += "\nby redisdl) into specified or default redis."
        usage += "\n\nIf FILE is omitted standard input is read."
    elif help == DUMP:
        usage = "Usage: %prog [options]"
        usage += "\n\nDump data from specified or default redis."
        usage += "\n\nIf no output file is specified, dump to standard output."
    else:
        usage = "Usage: %prog [options]"
        usage += "\n       %prog -l [options] [FILE]"
        usage += "\n\nDump data from redis or load data into redis."
        usage += "\n\nIf input or output file is specified, dump to standard output and load"
        usage += "\nfrom standard input."
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('-H', '--host', help='connect to HOST (default localhost)')
    parser.add_option('-p', '--port', help='connect to PORT (default 6379)')
    parser.add_option('-s', '--socket', help='connect to SOCKET')
    parser.add_option('-w', '--password', help='connect with PASSWORD')
    if help == DUMP:
        parser.add_option('-d', '--db', help='dump DATABASE (0-N, default 0)')
        parser.add_option('-k', '--keys', help='dump only keys matching specified glob-style pattern')
        parser.add_option('-o', '--output', help='write to OUTPUT instead of stdout')
        parser.add_option('-y', '--pretty', help='split output on multiple lines and indent it', action='store_true')
        parser.add_option('-E', '--encoding', help='set encoding to use while decoding data from redis', default='utf-8')
    elif help == LOAD:
        parser.add_option('-d', '--db', help='load into DATABASE (0-N, default 0)')
        parser.add_option('-e', '--empty', help='delete all keys in destination db prior to loading', action='store_true')
        parser.add_option('-E', '--encoding', help='set encoding to use while encoding data to redis', default='utf-8')
        parser.add_option('-B', '--backend', help='use specified streaming backend')
        parser.add_option('-A', '--use-expireat', help='use EXPIREAT rather than TTL/EXPIRE', action='store_true')
    else:
        parser.add_option('-l', '--load', help='load data into redis (default is to dump data from redis)', action='store_true')
        parser.add_option('-d', '--db', help='dump or load into DATABASE (0-N, default 0)')
        parser.add_option('-k', '--keys', help='dump only keys matching specified glob-style pattern')
        parser.add_option('-o', '--output', help='write to OUTPUT instead of stdout (dump mode only)')
        parser.add_option('-y', '--pretty', help='split output on multiple lines and indent it (dump mode only)', action='store_true')
        parser.add_option('-e', '--empty', help='delete all keys in destination db prior to loading (load mode only)', action='store_true')
        parser.add_option('-E', '--encoding', help='set encoding to use while decoding data from redis', default='utf-8')
        parser.add_option('-A', '--use-expireat', help='use EXPIREAT rather than TTL/EXPIRE', action='store_true')
        parser.add_option('-B', '--backend', help='use specified streaming backend (load mode only)')
    options, args = parser.parse_args()

    if hasattr(options, 'load') and options.load:
        action = LOAD

    if action == DUMP:
        if len(args) > 0:
            parser.print_help()
            exit(4)
        do_dump(options)
    else:
        if len(args) > 1:
            parser.print_help()
            exit(4)
        do_load(options, args)


if __name__ == '__main__':
    main()