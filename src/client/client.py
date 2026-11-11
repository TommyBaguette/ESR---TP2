import socket
import threading
import argparse
import sys
import os
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.common.protocol import (
    send_msg, recv_msg,
    MSG_HELLO, MSG_HELLO_ACK, MSG_HELLO_REJECT,
    MSG_PING, MSG_PONG,
    MSG_ROUTING_UPDATE, MSG_JOIN_STREAM, MSG_JOIN_ACK, MSG_LEAVE_STREAM
)


class StreamClient:
    def __init__(self, client_id: str, access_node_ip: str, access_node_port: int):
        self.client_id = client_id
        self.access_node_ip = access_node_ip
        self.access_node_port = access_node_port
        
        self.sock = None
        self.running = False
        self.known_streams = {}  # {stream_id: custo}
        self.active_stream = None

    def connect(self) -> bool:
        print(f"[{self.client_id}] A ligar ao ponto de acesso local ({self.access_node_ip}:{self.access_node_port})...")
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(4.0)
            self.sock.connect((self.access_node_ip, self.access_node_port))
            self.sock.settimeout(None)

            # Apresenta-se explicitamente com o papel 'client'
            send_msg(self.sock, {
                "type": MSG_HELLO,
                "node_id": self.client_id,
                "role": "client",
                "control_port": 0  # Cliente não abre socket de escuta para terceiros
            })

            reply = recv_msg(self.sock)
            if reply and reply.get("type") == MSG_HELLO_ACK:
                print(f"[{self.client_id}] Conectado com sucesso ao nó de acesso!")
                return True
            elif reply and reply.get("type") == MSG_HELLO_REJECT:
                print(f"[{self.client_id}] Ligação rejeitada: {reply.get('reason')}")
                return False
        except Exception as e:
            print(f"[{self.client_id}] Falha ao contactar nó de acesso: {e}")
            return False
        return False

    def join(self, stream_id: str):
        if not self.sock:
            print(f"[{self.client_id}] Erro: Sem ligação ativa ao nó de acesso.")
            return

        print(f"[{self.client_id}] A subscrever stream {stream_id}...")
        send_msg(self.sock, {
            "type": MSG_JOIN_STREAM,
            "flow_id": stream_id,
            "requester_id": self.client_id
        })
        self.active_stream = stream_id

    def leave(self):
        if not self.sock or not self.active_stream:
            return

        print(f"[{self.client_id}] A cancelar subscrição de {self.active_stream}...")
        send_msg(self.sock, {
            "type": MSG_LEAVE_STREAM,
            "flow_id": self.active_stream,
            "requester_id": self.client_id
        })
        self.active_stream = None

    def _listen_loop(self):
        while self.running:
            try:
                msg = recv_msg(self.sock)
                if not msg:
                    print(f"[{self.client_id}] Ligação ao nó de acesso perdida.")
                    break

                m_type = msg.get("type")

                if m_type == MSG_PING:
                    send_msg(self.sock, {"type": MSG_PONG, "node_id": self.client_id})

                elif m_type == MSG_ROUTING_UPDATE:
                    routes = msg.get("routes", {})
                    self.known_streams = routes
                    print(f"\n[{self.client_id}] Catálogo de streams atualizado: {list(routes.keys())}")

                elif m_type == MSG_JOIN_ACK:
                    print(f"\n[{self.client_id}] Stream {msg.get('flow_id')} subscrito com sucesso! Pronto para receber dados.")

            except Exception:
                break
        self.running = False

    def start(self):
        if not self.connect():
            return

        self.running = True
        threading.Thread(target=self._listen_loop, daemon=True).start()

        time.sleep(0.5)
        print(f"\n[{self.client_id}] Comandos do leitor:")
        print("  list          -> Ver canais/streams disponíveis")
        print("  join <stream> -> Subscrever e ver canal")
        print("  leave         -> Parar reprodução\n")

        while self.running:
            try:
                cmd = input().strip().split()
                if not cmd:
                    continue
                if cmd[0] == "list":
                    print(f"--- Streams Disponíveis ---")
                    for s_id in self.known_streams.keys():
                        print(f"  > {s_id}")
                    print("---------------------------")
                elif cmd[0] == "join" and len(cmd) > 1:
                    self.join(cmd[1])
                elif cmd[0] == "leave":
                    self.leave()
            except (EOFError, KeyboardInterrupt):
                break

        self.leave()
        if self.sock:
            self.sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente Leitor de Streaming OTT")
    parser.add_argument("--id", required=True, help="Identificador do cliente (ex: C1, C2)")
    parser.add_argument("--access-ip", required=True, help="IP do ONode local (ponto de acesso)")
    parser.add_argument("--access-port", type=int, default=5000, help="Porto TCP do ONode")
    args = parser.parse_args()

    client = StreamClient(args.id, args.access_ip, args.access_port)
    client.start()