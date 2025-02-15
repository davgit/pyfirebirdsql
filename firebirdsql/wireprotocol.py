##############################################################################
# Copyright (c) 2009-2014 Hajime Nakagami<nakagami@gmail.com>
# All rights reserved.
# Licensed under the New BSD License
# (http://www.freebsd.org/copyright/freebsd-license.html)
#
# Python DB-API 2.0 module for Firebird. 
##############################################################################
from __future__ import print_function
import sys
import os
import socket
import xdrlib, time, datetime, decimal, struct, select
from firebirdsql.fberrmsgs import messages
from firebirdsql import (DisconnectByPeer,
    DatabaseError, InternalError, OperationalError,
    ProgrammingError, IntegrityError, DataError, NotSupportedError,
)
from firebirdsql.consts import *
from firebirdsql.utils import *
from firebirdsql import srp
from firebirdsql.arc4 import Arc4

DEBUG = False

def DEBUG_OUTPUT(*argv):
    if not DEBUG:
        return
    for s in argv:
        print(s, end=' ', file=sys.stderr)
    print(file=sys.stderr)

INFO_SQL_SELECT_DESCRIBE_VARS = bs([
    isc_info_sql_select,
    isc_info_sql_describe_vars,
    isc_info_sql_sqlda_seq,
    isc_info_sql_type,
    isc_info_sql_sub_type,
    isc_info_sql_scale,
    isc_info_sql_length,
    isc_info_sql_null_ind,
    isc_info_sql_field,
    isc_info_sql_relation,
    isc_info_sql_owner,
    isc_info_sql_alias,
    isc_info_sql_describe_end])

def convert_date(v):  # Convert datetime.date to BLR format data
    i = v.month + 9
    jy = v.year + (i // 12) -1
    jm = i % 12
    c = jy // 100
    jy -= 100 * c
    j = (146097*c) // 4 + (1461*jy) // 4 + (153*jm+2) // 5 + v.day - 678882
    return bint_to_bytes(j, 4)

def convert_time(v):  # Convert datetime.time to BLR format time
    t = (v.hour*3600 + v.minute*60 + v.second) *10000 + v.microsecond // 100
    return bint_to_bytes(t, 4)

def convert_timestamp(v):   # Convert datetime.datetime to BLR format timestamp
    return convert_date(v.date()) + convert_time(v.time())

def wire_operation(fn):
    if not DEBUG:
        return fn
    def f(*args, **kwargs):
        DEBUG_OUTPUT('<--', fn, '-->')
        r = fn(*args, **kwargs)
        return r
    return f


class WireProtocol(object):
    buffer_length = 1024

    op_connect = 1
    op_exit = 2
    op_accept = 3
    op_reject = 4
    op_protocol = 5
    op_disconnect = 6
    op_response = 9
    op_attach = 19
    op_create = 20
    op_detach = 21
    op_transaction = 29
    op_commit = 30
    op_rollback = 31
    op_open_blob = 35
    op_get_segment = 36
    op_put_segment = 37
    op_close_blob = 39
    op_info_database = 40
    op_info_transaction = 42
    op_batch_segments = 44
    op_que_events = 48
    op_cancel_events = 49
    op_commit_retaining = 50
    op_event = 52
    op_connect_request = 53
    op_aux_connect = 53
    op_create_blob2 = 57
    op_allocate_statement = 62
    op_execute = 63
    op_exec_immediate = 64
    op_fetch = 65
    op_fetch_response = 66
    op_free_statement = 67
    op_prepare_statement = 68
    op_info_sql = 70
    op_dummy = 71
    op_execute2 = 76
    op_sql_response = 78
    op_drop_database = 81
    op_service_attach = 82
    op_service_detach = 83
    op_service_info = 84
    op_service_start = 85
    op_rollback_retaining = 86
    # FB3
    op_update_account_info = 87
    op_authenticate_user = 88
    op_partial = 89
    op_trusted_auth = 90
    op_cancel = 91
    op_cont_auth = 92
    op_ping = 93
    op_accept_data = 94
    op_abort_aux_connection = 95
    op_crypt = 96
    op_crypt_key_callback = 97
    op_cond_accept = 98

    def recv_channel(self, nbytes, word_alignment=False):
        n = nbytes
        if word_alignment and (n % 4):
            n += 4 - nbytes % 4  # 4 bytes word alignment
        r = bs([])
        while n:
            if (self.timeout is not None
                and select.select([self.sock._sock], [], [], self.timeout)[0] == []):
                break
            b = self.sock.recv(n)
            if not b:
                break
            r += b
            n -= len(b)
        if len(r) < nbytes:
            raise OperationalError('Can not recv() packets')
        return r[:nbytes]

    def str_to_bytes(self, s):
        "convert str to bytes"
        if (PYTHON_MAJOR_VER == 3 or
                (PYTHON_MAJOR_VER == 2 and type(s)==unicode)):
            return s.encode(charset_map.get(self.charset, self.charset))
        return s

    def bytes_to_str(self, b):
        "convert bytes array to raw string"
        if PYTHON_MAJOR_VER == 3:
            return b.decode(charset_map.get(self.charset, self.charset))
        return b

    def bytes_to_ustr(self, b):
        "convert bytes array to unicode string"
        return b.decode(charset_map.get(self.charset, self.charset))

    def _parse_status_vector(self):
        sql_code = 0
        gds_codes = set()
        message = ''
        n = bytes_to_bint(self.recv_channel(4))
        while n != isc_arg_end:
            if n == isc_arg_gds:
                gds_code = bytes_to_bint(self.recv_channel(4))
                if gds_code:
                    gds_codes.add(gds_code)
                    message += messages.get(gds_code, '@1')
                    num_arg = 0
            elif n == isc_arg_number:
                num = bytes_to_bint(self.recv_channel(4))
                if gds_code == 335544436:
                    sql_code = num
                num_arg += 1
                message = message.replace('@' + str(num_arg), str(num))
            elif (n == isc_arg_string or
                    n == isc_arg_interpreted
                    or n == isc_arg_sql_state):
                nbytes = bytes_to_bint(self.recv_channel(4))
                s = str(self.recv_channel(nbytes, word_alignment=True))
                num_arg += 1
                message = message.replace('@' + str(num_arg), s)
            elif n == isc_arg_sql_state:
                nbytes = bytes_to_bint(self.recv_channel(4))
                s = str(self.recv_channel(nbytes, word_alignment=True))
            n = bytes_to_bint(self.recv_channel(4))

        return (gds_codes, sql_code, message)


    def _parse_op_response(self):
        b = self.recv_channel(16)
        h = bytes_to_bint(b[0:4])         # Object handle
        oid = b[4:12]                       # Object ID
        buf_len = bytes_to_bint(b[12:])   # buffer length
        buf = self.recv_channel(buf_len, word_alignment=True)

        (gds_codes, sql_code, message) = self._parse_status_vector()
        if sql_code or message:
            raise OperationalError(message, gds_codes, sql_code)

        return (h, oid, buf)

    def _parse_op_event(self):
        b = self.recv_channel(4096) # too large TODO: read step by step
        # TODO: parse event name
        db_handle = bytes_to_bint(b[0:4])
        event_id = bytes_to_bint(b[-4:])

        return (db_handle, event_id, {})

    def _create_blob(self, trans_handle, b):
        self._op_create_blob2(trans_handle)
        (blob_handle, blob_id, buf) = self._op_response()

        i = 0
        while i < len(b):
            self._op_put_segment(blob_handle, b[i:i+BLOB_SEGMENT_SIZE])
            (h, oid, buf) = self._op_response()
            i += BLOB_SEGMENT_SIZE

        self._op_close_blob(blob_handle)
        (h, oid, buf) = self._op_response()
        return blob_id

    def params_to_blr(self, trans_handle, params):
        "Convert parameter array to BLR and values format."
        ln = len(params) * 2
        blr = bs([5, 2, 4, 0, ln & 255, ln >> 8])
        values = bs([])
        for p in params:
            t = type(p)
            if ((PYTHON_MAJOR_VER == 2 and type(p) == unicode) or
                (PYTHON_MAJOR_VER == 3 and type(p) == str)):
                p = self.str_to_bytes(p)
                t = type(p)
            if ((PYTHON_MAJOR_VER == 2 and t == str) or
                (PYTHON_MAJOR_VER == 3 and t == bytes)):
                if len(p) > MAX_CHAR_LENGTH:
                    v = self._create_blob(trans_handle, p)
                    blr += bs([9, 0])
                else:
                    v = p
                    nbytes = len(v)
                    pad_length = ((4-nbytes) & 3)
                    v += bs([0]) * pad_length
                    blr += bs([14, nbytes & 255, nbytes >> 8])
            elif t == int:
                v = bint_to_bytes(p, 4)
                blr += bs([8, 0])    # blr_long
            elif t == float and p == float("inf"):
                v = b'\x7f\x80\x00\x00'
                blr += bs([10])
            elif t == decimal.Decimal or t == float:
                if t == float:
                    p = decimal.Decimal(str(p))
                (sign, digits, exponent) = p.as_tuple()
                v = 0
                ln = len(digits)
                for i in range(ln):
                    v += digits[i] * (10 ** (ln -i-1))
                if sign:
                    v *= -1
                v = bint_to_bytes(v, 8)
                if exponent < 0:
                    exponent += 256
                blr += bs([16, exponent])
            elif t == datetime.date:
                v = convert_date(p)
                blr += bs([12])
            elif t == datetime.time:
                v = convert_time(p)
                blr += bs([13])
            elif t == datetime.datetime:
                v = convert_timestamp(p)
                blr += bs([35])
            elif t == bool:
                v = bs([1, 0, 0, 0]) if p else bs([0, 0, 0, 0])
                blr += bs([23])
            else:   # fallback, convert to string
                if p is None:
                    v = bs([])
                else:
                    p = p.__repr__()
                    if (PYTHON_MAJOR_VER==3 or
                        (PYTHON_MAJOR_VER == 2 and type(p)==unicode)):
                        p = self.str_to_bytes(p)
                    v = p
                nbytes = len(v)
                pad_length = ((4-nbytes) & 3)
                v += bs([0]) * pad_length
                blr += bs([14, nbytes & 255, nbytes >> 8])
            values += v
            blr += bs([7, 0])
            values += bs([0]) * 4 if p != None else bs([0xff,0xff,0xff,0xff])
        blr += bs([255, 76])    # [blr_end, blr_eoc]
        return blr, values

    def uid(self, auth_plugin_list, wire_crypt):
        def pack_cnct_param(k, v):
            if k != CNCT_specific_data:
                return bs([k] + [len(v)]) + v
            # specific_data split per 254 bytes
            b = b''
            i = 0
            while len(v) > 254:
                b += bs([k, 255, i]) + v[:254]
                v = v[254:]
                i += 1
            b += bs([k, len(v)+1, i]) + v
            return b

        if sys.platform == 'win32':
            user = os.environ['USERNAME']
            hostname = os.environ['COMPUTERNAME']
        else:
            user = os.environ.get('USER', '')
            hostname = socket.gethostname()
        r = b''

        if auth_plugin_list:
            specific_data = None
            if auth_plugin_list[0] == 'Srp':
                self.client_public_key, self.client_private_key = \
                                    srp.client_seed()
                specific_data = bytes_to_hex(
                                    srp.long2bytes(self.client_public_key))
            elif auth_plugin_list[0] == 'Legacy_Auth':
                enc_pass = get_crypt(self.password)
                if enc_pass:
                    specific_data = self.str_to_bytes(enc_pass)
            else:
                if not isinstance(auth_plugin_list, tuple):
                    raise OperationalError("Auth plugin list need tuple.")
                else:
                    raise OperationalError(
                        "Unknown auth plugin name '%s'" % (auth_plugin_list[0]))
            auth_plugin_list = [s.encode('utf-8') for s in auth_plugin_list]
            self.plugin_name = auth_plugin_list[0]
            self.plugin_list = b','.join(auth_plugin_list)
            if wire_crypt:
                client_crypt = int_to_bytes(1, 4)
            else:
                client_crypt = int_to_bytes(0, 4)

        if auth_plugin_list:
            r += pack_cnct_param(CNCT_login,
                                self.str_to_bytes(self.user.upper()))
            r += pack_cnct_param(CNCT_plugin_name, self.plugin_name)
            r += pack_cnct_param(CNCT_plugin_list, self.plugin_list)
            if specific_data:
                r += pack_cnct_param(CNCT_specific_data, specific_data)
            r += pack_cnct_param(CNCT_client_crypt, client_crypt)
        r += pack_cnct_param(CNCT_user, self.str_to_bytes(user))
        r += pack_cnct_param(CNCT_host, self.str_to_bytes(hostname))
        r += pack_cnct_param(CNCT_user_verification, b'')
        return r

    @wire_operation
    def _op_connect(self, auth_plugin_list, wire_crypt):
        arch_type = 36
        min_arch_type = 0
        max_arch_type = 5
        protocol_version_understood_count = 4
        # accept_type = 5
#        more_protocol = hex_to_bytes('ffff800b00000001000000000000000500000004ffff800c00000001000000000000000500000006ffff800d00000001000000000000000500000008')
        # accept_type = 4
        more_protocol = hex_to_bytes('ffff800b00000001000000000000000400000004ffff800c00000001000000000000000400000006ffff800d00000001000000000000000400000008')
        p = xdrlib.Packer()
        p.pack_int(self.op_connect)
        p.pack_int(self.op_attach)
        p.pack_int(3)   # CONNECT_VERSION
        p.pack_int(arch_type)
        p.pack_string(self.str_to_bytes(self.filename if self.filename else ''))
        p.pack_int(protocol_version_understood_count)
        p.pack_bytes(self.uid(auth_plugin_list, wire_crypt))
        p.pack_int(PROTOCOL_VERSION10)
        p.pack_int(1)   # Protocol Arch type (Generic = 1)
        p.pack_int(min_arch_type)
        p.pack_int(max_arch_type)
        p.pack_int(2)   # Preference weight
        self.sock.send(p.get_buffer()+more_protocol)

    @wire_operation
    def _op_create(self, page_size=4096):
        dpb = bs([1])
        s = self.str_to_bytes(self.charset)
        dpb += bs([isc_dpb_set_db_charset, len(s)]) + s
        dpb += bs([isc_dpb_lc_ctype, len(s)]) + s
        s = self.str_to_bytes(self.user)
        dpb += bs([isc_dpb_user_name, len(s)]) + s
        if self.accept_version < PROTOCOL_VERSION13:
            enc_pass = get_crypt(self.password)
            if self.accept_version == PROTOCOL_VERSION10 or not enc_pass:
                s = self.str_to_bytes(self.password)
                dpb += bs([isc_dpb_password, len(s)]) + s
            else:
                enc_pass = self.str_to_bytes(enc_pass)
                dpb += bs([isc_dpb_password_enc, len(enc_pass)]) + enc_pass
        if self.role:
            s = self.str_to_bytes(self.role)
            dpb += bs([isc_dpb_sql_role_name, len(s)]) + s
        dpb += bs([isc_dpb_sql_dialect, 4]) + int_to_bytes(3, 4)
        dpb += bs([isc_dpb_force_write, 4]) + int_to_bytes(1, 4)
        dpb += bs([isc_dpb_overwrite, 4]) + int_to_bytes(1, 4)
        dpb += bs([isc_dpb_page_size, 4]) + int_to_bytes(page_size, 4)
        p = xdrlib.Packer()
        p.pack_int(self.op_create)
        p.pack_int(0)                       # Database Object ID
        p.pack_string(self.str_to_bytes(self.filename))
        p.pack_bytes(dpb)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_accept(self):
        b = self.recv_channel(4)
        while bytes_to_bint(b) == self.op_dummy:
            b = self.recv_channel(4)
        if bytes_to_bint(b) == self.op_reject:
            raise OperationalError('Connection is rejected')

        op_code = bytes_to_bint(b)
        if op_code == self.op_response:
            return self._parse_op_response()    # error occured

        b = self.recv_channel(12)
        self.accept_version = byte_to_int(b[3])
        self.accept_architecture = bytes_to_bint(b[4:8])
        self.accept_type =  bytes_to_bint(b[8:])

        if op_code == self.op_cond_accept or op_code == self.op_accept_data:
            read_length = 0

            ln = bytes_to_bint(self.recv_channel(4))
            data = self.recv_channel(ln)
            read_length += 4 + ln
            if read_length % 4:
                self.recv_channel(4 - read_length % 4) # padding
                read_length += 4 - read_length % 4

            ln = bytes_to_bint(self.recv_channel(4))
            self.plugin_name = self.recv_channel(ln)
            read_length += 4 + ln
            if read_length % 4:
                self.recv_channel(4 - read_length % 4) # padding
                read_length += 4 - read_length % 4

            is_authenticated = bytes_to_bint(self.recv_channel(4))
            read_length += 4
            ln = bytes_to_bint(self.recv_channel(4))
            keys = self.recv_channel(ln)
            read_length += 4 + ln
            if read_length % 4:
                self.recv_channel(4 - read_length % 4) # padding
                read_length += 4 - read_length % 4

            if self.plugin_name == b'Legacy_Auth' and is_authenticated == 0:
                raise OperationalError('Unauthorized')

            if self.plugin_name == b'Srp':
                ln = bytes_to_int(data[:2])
                server_salt = data[2:ln+2]
                server_public_key = srp.bytes2long(
                                        hex_to_bytes(data[4+ln:]))

                client_proof, auth_key = srp.client_proof(
                                        self.str_to_bytes(self.user.upper()),
                                        self.str_to_bytes(self.password),
                                        server_salt,
                                        self.client_public_key,
                                        server_public_key,
                                        self.client_private_key)
                # send op_cont_auth
                p = xdrlib.Packer()
                p.pack_int(self.op_cont_auth)
                p.pack_string(bytes_to_hex(client_proof))
                p.pack_bytes(self.plugin_name)
                p.pack_bytes(self.plugin_list)
                p.pack_bytes(b'')
                self.sock.send(p.get_buffer())
                (h, oid, buf) = self._op_response()

                # op_crypt: plugin[Arc4] key[Symmetric]
                p = xdrlib.Packer()
                p.pack_int(self.op_crypt)
                p.pack_string(b'Arc4')
                p.pack_string(b'Symmetric')
                self.sock.send(p.get_buffer())
                self.sock.set_translator(Arc4(auth_key), Arc4(auth_key))
                (h, oid, buf) = self._op_response()
        else:
            assert op_code == self.op_accept

    @wire_operation
    def _op_attach(self):
        dpb = bs([1])
        s = self.str_to_bytes(self.charset)
        dpb += bs([isc_dpb_lc_ctype, len(s)]) + s
        s = self.str_to_bytes(self.user)
        dpb += bs([isc_dpb_user_name, len(s)]) + s
        if self.accept_version < PROTOCOL_VERSION13:
            enc_pass = get_crypt(self.password)
            if self.accept_version == PROTOCOL_VERSION10 or not enc_pass:
                s = self.str_to_bytes(self.password)
                dpb += bs([isc_dpb_password, len(s)]) + s
            else:
                enc_pass = self.str_to_bytes(enc_pass)
                dpb += bs([isc_dpb_password_enc, len(enc_pass)]) + enc_pass
        if self.role:
            s = self.str_to_bytes(self.role)
            dpb += bs([isc_dpb_sql_role_name, len(s)]) + s
        p = xdrlib.Packer()
        p.pack_int(self.op_attach)
        p.pack_int(0)                       # Database Object ID
        p.pack_string(self.str_to_bytes(self.filename))
        p.pack_bytes(dpb)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_drop_database(self):
        if self.db_handle is None:
            raise OperationalError('_op_drop_database() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_drop_database)
        p.pack_int(self.db_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_service_attach(self):
        dpb = bs([2,2])
        s = self.str_to_bytes(self.user)
        dpb += bs([isc_spb_user_name, len(s)]) + s
        s = self.str_to_bytes(self.password)
        dpb += bs([isc_spb_password, len(s)]) + s
        dpb += bs([isc_spb_dummy_packet_interval,0x04,0x78,0x0a,0x00,0x00])
        p = xdrlib.Packer()
        p.pack_int(self.op_service_attach)
        p.pack_int(0)
        p.pack_string(self.str_to_bytes('service_mgr'))
        p.pack_bytes(dpb)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_service_info(self, param, item, buffer_length=512):
        if self.db_handle is None:
            raise OperationalError('_op_service_info() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_service_info)
        p.pack_int(self.db_handle)
        p.pack_int(0)
        p.pack_bytes(param)
        p.pack_bytes(item)
        p.pack_int(buffer_length)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_service_start(self, param):
        if self.db_handle is None:
            raise OperationalError('_op_service_start() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_service_start)
        p.pack_int(self.db_handle)
        p.pack_int(0)
        p.pack_bytes(param)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_service_detach(self):
        if self.db_handle is None:
            raise OperationalError('_op_service_detach() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_service_detach)
        p.pack_int(self.db_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_info_database(self, b):
        if self.db_handle is None:
            raise OperationalError('_op_info_database() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_info_database)
        p.pack_int(self.db_handle)
        p.pack_int(0)
        p.pack_bytes(b)
        p.pack_int(self.buffer_length)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_transaction(self, tpb):
        if self.db_handle is None:
            raise OperationalError('_op_transaction() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_transaction)
        p.pack_int(self.db_handle)
        p.pack_bytes(tpb)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_commit(self, trans_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_commit)
        p.pack_int(trans_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_commit_retaining(self, trans_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_commit_retaining)
        p.pack_int(trans_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_rollback(self, trans_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_rollback)
        p.pack_int(trans_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_rollback_retaining(self, trans_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_rollback_retaining)
        p.pack_int(trans_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_allocate_statement(self):
        if self.db_handle is None:
            raise OperationalError('_op_allocate_statement() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_allocate_statement)
        p.pack_int(self.db_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_info_transaction(self, trans_handle, b):
        p = xdrlib.Packer()
        p.pack_int(self.op_info_transaction)
        p.pack_int(trans_handle)
        p.pack_int(0)
        p.pack_bytes(b)
        p.pack_int(self.buffer_length)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_free_statement(self, stmt_handle, mode):
        p = xdrlib.Packer()
        p.pack_int(self.op_free_statement)
        p.pack_int(stmt_handle)
        p.pack_int(mode)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_prepare_statement(self, stmt_handle, trans_handle, query, option_items=bs([])):
        desc_items = option_items + bs([isc_info_sql_stmt_type])+INFO_SQL_SELECT_DESCRIBE_VARS
        p = xdrlib.Packer()
        p.pack_int(self.op_prepare_statement)
        p.pack_int(trans_handle)
        p.pack_int(stmt_handle)
        p.pack_int(3)   # dialect = 3
        p.pack_string(self.str_to_bytes(query))
        p.pack_bytes(desc_items)
        p.pack_int(self.buffer_length)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_info_sql(self, stmt_handle, vars):
        p = xdrlib.Packer()
        p.pack_int(self.op_info_sql)
        p.pack_int(stmt_handle)
        p.pack_int(0)
        p.pack_bytes(vars)
        p.pack_int(self.buffer_length)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_execute(self, stmt_handle, trans_handle, params):
        p = xdrlib.Packer()
        p.pack_int(self.op_execute)
        p.pack_int(stmt_handle)
        p.pack_int(trans_handle)

        if len(params) == 0:
            p.pack_bytes(bs([]))
            p.pack_int(0)
            p.pack_int(0)
            self.sock.send(p.get_buffer())
        else:
            (blr, values) = self.params_to_blr(trans_handle, params)
            p.pack_bytes(blr)
            p.pack_int(0)
            p.pack_int(1)
            self.sock.send(p.get_buffer() + values)

    @wire_operation
    def _op_execute2(self, stmt_handle, trans_handle, params, output_blr):
        p = xdrlib.Packer()
        p.pack_int(self.op_execute2)
        p.pack_int(stmt_handle)
        p.pack_int(trans_handle)

        if len(params) == 0:
            p.pack_bytes(bs([]))
            p.pack_int(0)
            p.pack_int(0)
            self.sock.send(p.get_buffer())
        else:
            (blr, values) = self.params_to_blr(trans_handle, params)
            p.pack_bytes(blr)
            p.pack_int(0)
            p.pack_int(1)
            self.sock.send(p.get_buffer() + values)

        p = xdrlib.Packer()
        p.pack_bytes(output_blr)
        p.pack_int(0)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_exec_immediate(self, trans_handle, query):
        if self.db_handle is None:
            raise OperationalError('_op_exec_immediate() Invalid db handle')
        desc_items = bs([])
        p = xdrlib.Packer()
        p.pack_int(self.op_exec_immediate)
        p.pack_int(trans_handle)
        p.pack_int(self.db_handle)
        p.pack_int(3)   # dialect = 3
        p.pack_string(self.str_to_bytes(query))
        p.pack_bytes(desc_items)
        p.pack_int(self.buffer_length)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_fetch(self, stmt_handle, blr):
        p = xdrlib.Packer()
        p.pack_int(self.op_fetch)
        p.pack_int(stmt_handle)
        p.pack_bytes(blr)
        p.pack_int(0)
        p.pack_int(400)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_fetch_response(self, stmt_handle, xsqlda):
        b = self.recv_channel(4)
        while bytes_to_bint(b) == self.op_dummy:
            b = self.recv_channel(4)
        if bytes_to_bint(b) == self.op_response:
            return self._parse_op_response()    # error occured
        if bytes_to_bint(b) != self.op_fetch_response:
            raise InternalError
        b = self.recv_channel(8)
        status = bytes_to_bint(b[:4])
        count = bytes_to_bint(b[4:8])
        rows = []
        while count:
            r = [None] * len(xsqlda)
            for i in range(len(xsqlda)):
                x = xsqlda[i]
                if x.io_length() < 0:
                    b = self.recv_channel(4)
                    ln = bytes_to_bint(b)
                else:
                    ln = x.io_length()
                raw_value = self.recv_channel(ln, word_alignment=True)
                if self.recv_channel(4) == bs([0]) * 4: # Not NULL
                    r[i] = x.value(raw_value)
            rows.append(r)
            b = self.recv_channel(12)
            op = bytes_to_bint(b[:4])
            status = bytes_to_bint(b[4:8])
            count = bytes_to_bint(b[8:])
        return rows, status != 100

    @wire_operation
    def _op_detach(self):
        if self.db_handle is None:
            raise OperationalError('_op_detach() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_detach)
        p.pack_int(self.db_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_open_blob(self, blob_id, trans_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_open_blob)
        p.pack_int(trans_handle)
        self.sock.send(p.get_buffer() + blob_id)

    @wire_operation
    def _op_create_blob2(self, trans_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_create_blob2)
        p.pack_int(0)
        p.pack_int(trans_handle)
        p.pack_int(0)
        p.pack_int(0)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_get_segment(self, blob_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_get_segment)
        p.pack_int(blob_handle)
        p.pack_int(self.buffer_length)
        p.pack_int(0)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_put_segment(self, blob_handle, b):
        p = xdrlib.Packer()
        p.pack_int(self.op_put_segment)
        p.pack_int(blob_handle)
        p.pack_int(len(b))
        p.pack_int(len(b))
        self.sock.send(p.get_buffer() + b)

    @wire_operation
    def _op_batch_segments(self, blob_handle, seg_data):
        ln = len(seg_data)
        p = xdrlib.Packer()
        p.pack_int(self.op_batch_segments)
        p.pack_int(blob_handle)
        p.pack_int(ln + 2)
        p.pack_int(ln + 2)
        pad_length = ((4-(ln+2)) & 3)
        self.sock.send(p.get_buffer() 
                + int_to_bytes(ln, 2) + seg_data + bs([0])*pad_length)

    @wire_operation
    def _op_close_blob(self, blob_handle):
        p = xdrlib.Packer()
        p.pack_int(self.op_close_blob)
        p.pack_int(blob_handle)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_que_events(self, event_names, ast, args, event_id):
        if self.db_handle is None:
            raise OperationalError('_op_que_events() Invalid db handle')
        params = bs([1])
        for name, n in event_names.items():
            params += bs([len(name)])
            params += self.str_to_bytes(name)
            params += int_to_bytes(n, 4)
        p = xdrlib.Packer()
        p.pack_int(self.op_que_events)
        p.pack_int(self.db_handle)
        p.pack_bytes(params)
        p.pack_int(ast)
        p.pack_int(args)
        p.pack_int(event_id)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_cancel_events(self, event_id):
        if self.db_handle is None:
            raise OperationalError('_op_cancel_events() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_cancel_events)
        p.pack_int(self.db_handle)
        p.pack_int(event_id)
        self.sock.send(p.get_buffer())

    @wire_operation
    def _op_connect_request(self):
        if self.db_handle is None:
            raise OperationalError('_op_connect_request() Invalid db handle')
        p = xdrlib.Packer()
        p.pack_int(self.op_connect_request)
        p.pack_int(1)    # async
        p.pack_int(self.db_handle)
        p.pack_int(0)
        self.sock.send(p.get_buffer())

        b = self.recv_channel(4)
        while bytes_to_bint(b) == self.op_dummy:
            b = self.recv_channel(4)
        if bytes_to_bint(b) != self.op_response:
            raise InternalError

        h = bytes_to_bint(self.recv_channel(4))
        self.recv_channel(8)  # garbase
        ln = bytes_to_bint(self.recv_channel(4))
        ln += ln % 4    # padding
        family = bytes_to_bint(self.recv_channel(2))
        port = bytes_to_bint(self.recv_channel(2), u=True)
        b = self.recv_channel(4)
        ip_address = '.'.join([str(byte_to_int(c)) for c in b])
        ln -= 8
        self.recv_channel(ln)

        (gds_codes, sql_code, message) = self._parse_status_vector()
        if sql_code or message:
            raise OperationalError(message, gds_codes, sql_code)

        return (h, port, family, ip_address)

    @wire_operation
    def _op_response(self):
        b = self.recv_channel(4)
        if b is None:
            return
        while bytes_to_bint(b) == self.op_dummy:
            b = self.recv_channel(4)
        if bytes_to_bint(b) != self.op_response:
            raise InternalError
        return self._parse_op_response()

    @wire_operation
    def _op_event(self):
        b = self.recv_channel(4)
        while bytes_to_bint(b) == self.op_dummy:
            b = self.recv_channel(4)
        if bytes_to_bint(b) == self.op_response:
            return self._parse_op_response()
        if bytes_to_bint(b) == self.op_exit or bytes_to_bint(b) == self.op_exit:
            raise DisconnectByPeer
        if bytes_to_bint(b) != self.op_event:
            raise InternalError
        return self._parse_op_event()

    @wire_operation
    def _op_sql_response(self, xsqlda):
        b = self.recv_channel(4)
        while bytes_to_bint(b) == self.op_dummy:
            b = self.recv_channel(4)
        if bytes_to_bint(b) != self.op_sql_response:
            raise InternalError

        b = self.recv_channel(4)
        count = bytes_to_bint(b[:4])
        r = []
        if count == 0:
            return []
        for i in range(len(xsqlda)):
            x = xsqlda[i]
            if x.io_length() < 0:
                b = self.recv_channel(4)
                ln = bytes_to_bint(b)
            else:
                ln = x.io_length()
            raw_value = self.recv_channel(ln, word_alignment=True)
            if self.recv_channel(4) == bs([0]) * 4: # Not NULL
                r.append(x.value(raw_value))
            else:
                r.append(None)
        return r

    def _wait_for_event(self, timeout):
        event_names = {}
        event_id = 0
        while True:
            b4 = self.recv_channel(4)
            if b4 is None:
                return None
            op = bytes_to_bint(b4)
            if op == self.op_dummy:
                pass
            elif op == self.op_exit or op == self.op_disconnect:
                break
            elif op == self.op_event:
                db_handle = bytes_to_int(self.recv_channel(4))
                ln = bytes_to_bint(self.recv_channel(4))
                b = self.recv_channel(ln, word_alignment=True)
                assert byte_to_int(b[0]) == 1
                i = 1
                while i < len(b):
                    ln = byte_to_int(b[i])
                    s = self.connection.bytes_to_str(b[i+1:i+1+ln])
                    n = bytes_to_int(b[i+1+ln:i+1+ln+4])
                    event_names[s] = n
                    i += ln + 5
                self.recv_channel(8)  # ignore AST info

                event_id = bytes_to_bint(self.recv_channel(4))
                break
            else:
                raise InternalError

        return (event_id, event_names)
