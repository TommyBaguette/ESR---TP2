import json
import struct
import socket

# Tipos de Mensagens
MSG_BOOTSTRAP_REQ = "BOOTSTRAP_REQ"
MSG_BOOTSTRAP_REPLY = "BOOTSTRAP_REPLY"
MSG_HELLO = "HELLO"
MSG_HELLO_ACK = "HELLO_ACK"
MSG_HELLO_REJECT = "HELLO_REJECT"
MSG_PEER_SUGGEST = "PEER_SUGGEST"
MSG_PING = "PING"
MSG_PONG = "PONG"

MSG_ROUTING_UPDATE = "ROUTING_UPDATE"
MSG_JOIN_STREAM = "JOIN_STREAM"
MSG_JOIN_ACK = "JOIN_ACK"
MSG_LEAVE_STREAM = "LEAVE_STREAM"


def send_msg(sock: socket.socket, payload: dict) -> None:
    raw_data = json.dumps(payload).encode('utf-8')
    header = struct.pack('!I', len(raw_data))
    sock.sendall(header + raw_data)


def _recvall(sock: socket.socket, n_bytes: int) -> bytearray:
    data = bytearray()
    while len(data) < n_bytes:
        packet = sock.recv(n_bytes - len(data))
        if not packet:
            if len(data) == 0:
                return None
            raise ConnectionResetError("Conexão fechada a meio da leitura.")
        data.extend(packet)
    return data


def recv_msg(sock: socket.socket) -> dict:
    header = _recvall(sock, 4)
    if header is None:
        return None

    msg_len = struct.unpack('!I', header)[0]
    raw_payload = _recvall(sock, msg_len)
    if raw_payload is None:
        raise ConnectionResetError("Socket fechado antes de ler o payload completo.")

    return json.loads(raw_payload.decode('utf-8'))