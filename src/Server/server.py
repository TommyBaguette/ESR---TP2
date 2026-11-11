import socket
import threading
import argparse
import sys
import os
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.common.protocol import send_msg, recv_msg, MSG_BOOTSTRAP_REQ, MSG_BOOTSTRAP_REPLY
from src.overlay.neighbor_manager import NeighborManager
from src.overlay.routing import RoutingTable


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


class VideoServer:
    def __init__(self, server_id: str, control_port: int, bootstrap_ip: str, bootstrap_port: int, streams: list):
        self.server_id = server_id
        self.control_port = control_port
        self.local_ip = get_local_ip()
        self.bootstrap_ip = bootstrap_ip
        self.bootstrap_port = bootstrap_port

        self.routing_table = RoutingTable(self.server_id)
        # Papel "server": rejeita conexões diretas de clientes, fala só com ONodes
        self.neighbor_mgr = NeighborManager(self.server_id, "server", self.control_port, self.routing_table)
        self.running = False

        # Registo de todos os fluxos com Custo 0
        self.streams = streams
        for s in streams:
            self.routing_table.register_local_stream(s)

    def perform_bootstrap(self):
        print(f"[{self.server_id}:Server] A registar no Bootstrapper ({self.bootstrap_ip}:{self.bootstrap_port})...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(4.0)
            sock.connect((self.bootstrap_ip, self.bootstrap_port))
            
            send_msg(sock, {
                "type": MSG_BOOTSTRAP_REQ,
                "node_id": self.server_id,
                "control_port": self.control_port
            })

            reply = recv_msg(sock)
            sock.close()

            if reply and reply.get("type") == MSG_BOOTSTRAP_REPLY:
                peers = reply.get("peers", [])
                print(f"[{self.server_id}:Server] Encontrados {len(peers)} nós de infraestrutura.")
                self.neighbor_mgr.add_known_contacts(peers)
        except Exception as e:
            print(f"[{self.server_id}:Server] Aviso: Bootstrapper inacessível ({e})")

    def interactive_cli(self):
        time.sleep(1.0)
        print(f"\n[{self.server_id}:Server] Consola pronta:")
        print("  status -> Ver estado dos streams e subscritores a jusante\n")

        while self.running:
            try:
                cmd = input().strip().split()
                if not cmd:
                    continue
                if cmd[0] == "status":
                    print(f"--- Origens de Vídeo em {self.server_id} ---")
                    with self.routing_table.lock:
                        for fid, r in self.routing_table.routes.items():
                            print(f"  Stream: {fid} | Filhos Ativos: {list(r.children)}")
                    print("------------------------------------------")
            except (EOFError, KeyboardInterrupt):
                break

    def start(self):
        self.running = True
        self.neighbor_mgr.running = True

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("0.0.0.0", self.control_port))
        server_sock.listen(5)
        print(f"[{self.server_id}:Server] Servidor ativo em {self.local_ip}:{self.control_port}")
        print(f"[{self.server_id}:Server] A disponibilizar streams: {self.streams}")

        self.perform_bootstrap()

        threading.Thread(target=self.neighbor_mgr.heartbeat_and_maintenance_loop, daemon=True).start()
        threading.Thread(target=self.interactive_cli, daemon=True).start()

        try:
            while self.running:
                client_sock, addr = server_sock.accept()
                threading.Thread(
                    target=self.neighbor_mgr.handle_incoming_connection,
                    args=(client_sock, addr),
                    daemon=True
                ).start()
        except KeyboardInterrupt:
            print(f"\n[{self.server_id}:Server] A encerrar...")
        finally:
            self.running = False
            self.neighbor_mgr.running = False
            server_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor de Streaming OTT")
    parser.add_argument("--id", required=True, help="Identificador do servidor (ex: S1, S2)")
    parser.add_argument("--port", type=int, default=5000, help="Porto TCP de controlo")
    parser.add_argument("--bootstrap-ip", required=True, help="IP do Bootstrapper")
    parser.add_argument("--bootstrap-port", type=int, default=9000, help="Porto do Bootstrapper")
    parser.add_argument("--stream", nargs="+", required=True, help="Identificadores dos fluxos (ex: S1:A S1:B)")
    args = parser.parse_args()

    server = VideoServer(args.id, args.port, args.bootstrap_ip, args.bootstrap_port, args.stream)
    server.start()