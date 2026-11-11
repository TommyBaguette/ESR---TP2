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


class ONode:
    def __init__(self, node_id: str, control_port: int, bootstrap_ip: str, bootstrap_port: int):
        self.node_id = node_id
        self.control_port = control_port
        self.local_ip = get_local_ip()
        self.bootstrap_ip = bootstrap_ip
        self.bootstrap_port = bootstrap_port

        self.routing_table = RoutingTable(self.node_id)
        # ONode usa o papel "node", permitindo conetividade com servidores e outros ONodes
        self.neighbor_mgr = NeighborManager(self.node_id, "node", self.control_port, self.routing_table)
        self.running = False

    def perform_bootstrap(self):
        print(f"[{self.node_id}:ONode] A registar no Bootstrapper ({self.bootstrap_ip}:{self.bootstrap_port})...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(4.0)
            sock.connect((self.bootstrap_ip, self.bootstrap_port))
            
            send_msg(sock, {
                "type": MSG_BOOTSTRAP_REQ,
                "node_id": self.node_id,
                "control_port": self.control_port
            })

            reply = recv_msg(sock)
            sock.close()

            if reply and reply.get("type") == MSG_BOOTSTRAP_REPLY:
                peers = reply.get("peers", [])
                print(f"[{self.node_id}:ONode] Contactos recebidos do Bootstrap: {len(peers)}")
                self.neighbor_mgr.add_known_contacts(peers)
        except Exception as e:
            print(f"[{self.node_id}:ONode] Aviso: Não foi possível contactar bootstrapper ({e})")

    def interactive_cli(self):
        time.sleep(1.0)
        print(f"\n[{self.node_id}:ONode] Consola pronta:")
        print("  routes   -> Ver tabela de rotas e árvores ativas")
        print("  peers    -> Listar vizinhos TCP ativos\n")

        while self.running:
            try:
                cmd = input().strip().split()
                if not cmd:
                    continue
                if cmd[0] == "routes":
                    print(f"--- Rotas Overlay de {self.node_id} ---")
                    with self.routing_table.lock:
                        for fid, r in self.routing_table.routes.items():
                            print(f"  Stream: {fid} | Custo: {r.cost} | Pai: {r.parent} | Filhos: {list(r.children)} | Ativo: {r.is_active}")
                    print("---------------------------------------")
                elif cmd[0] == "peers":
                    print(f"--- Vizinhos Ativos de {self.node_id} ---")
                    with self.neighbor_mgr.lock:
                        for pid, info in self.neighbor_mgr.active_neighbors.items():
                            print(f"  {pid} -> {info['addr']} (Papel: {info.get('role')})")
                    print("---------------------------------------")
            except (EOFError, KeyboardInterrupt):
                break

    def start(self):
        self.running = True
        self.neighbor_mgr.running = True

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("0.0.0.0", self.control_port))
        server_sock.listen(10)
        print(f"[{self.node_id}:ONode] Escuta de controlo no IP {self.local_ip}:{self.control_port}")

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
            print(f"\n[{self.node_id}:ONode] A terminar...")
        finally:
            self.running = False
            self.neighbor_mgr.running = False
            server_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nó de Encaminhamento Overlay (ONode)")
    parser.add_argument("--id", required=True, help="Identificador único (ex: O1, O2)")
    parser.add_argument("--port", type=int, default=5000, help="Porto TCP de controlo")
    parser.add_argument("--bootstrap-ip", required=True, help="IP do Bootstrapper")
    parser.add_argument("--bootstrap-port", type=int, default=9000, help="Porto do Bootstrapper")
    args = parser.parse_args()

    onode = ONode(args.id, args.port, args.bootstrap_ip, args.bootstrap_port)
    onode.start()