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
    def __init__(self, node_id: str, control_port: int, bootstrap_ip: str, bootstrap_port: int, allowed_neighbors: list = None):
        self.node_id = node_id
        self.control_port = control_port
        self.data_port = control_port + 1
        self.local_ip = get_local_ip()
        self.bootstrap_ip = bootstrap_ip
        self.bootstrap_port = bootstrap_port

        self.routing_table = RoutingTable(self.node_id)
        self.neighbor_mgr = NeighborManager(
            self.node_id, "node", self.control_port, self.routing_table, allowed_neighbors
        )

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
                print(f"[{self.node_id}:ONode] Contactos obtidos do Bootstrap: {len(peers)}")
                self.neighbor_mgr.add_known_contacts(peers)
        except Exception as e:
            print(f"[{self.node_id}:ONode] Aviso: Bootstrapper inacessível ({e})")

    def _udp_forwarder_loop(self):
        self.udp_sock.bind(("0.0.0.0", self.data_port))
        print(f"[{self.node_id}:ONode] Motor UDP de reencaminhamento ativo no porto {self.data_port}")

        while self.running:
            try:
                data, _ = self.udp_sock.recvfrom(65535)
                if len(data) < 16:
                    continue

                flow_id = data[:16].decode('utf-8', errors='ignore').rstrip('\x00')

                dest_addrs = []
                with self.routing_table.lock:
                    entry = self.routing_table.routes.get(flow_id)
                    if entry and entry.children:
                        with self.neighbor_mgr.lock:
                            for child_id in entry.children:
                                if child_id in self.neighbor_mgr.active_neighbors:
                                    child_ip, child_cport = self.neighbor_mgr.active_neighbors[child_id]["addr"]
                                    child_dport = 5001 if child_cport == 0 else child_cport + 1
                                    dest_addrs.append((child_ip, child_dport))

                for addr in dest_addrs:
                    try:
                        self.udp_sock.sendto(data, addr)
                    except Exception:
                        pass

            except Exception:
                break

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
                            print(f"  Stream: {fid} | Custo RTT: {r.cost:.2f}ms | Pai: {r.parent} | Filhos: {list(r.children)} | Ativo: {r.is_active}")
                    print("---------------------------------------")
                elif cmd[0] == "peers":
                    print(f"--- Vizinhos Ativos de {self.node_id} ---")
                    with self.neighbor_mgr.lock:
                        for pid, info in self.neighbor_mgr.active_neighbors.items():
                            print(f"  {pid} -> {info['addr']} (RTT: {info['rtt']:.2f}ms, Papel: {info.get('role')})")
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
        print(f"[{self.node_id}:ONode] Controlo TCP em {self.local_ip}:{self.control_port}")

        self.perform_bootstrap()

        threading.Thread(target=self._udp_forwarder_loop, daemon=True).start()
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
            self.udp_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nó de Trânsito Overlay (ONode)")
    parser.add_argument("--id", required=True, help="Identificador único (ex: O1)")
    parser.add_argument("--port", type=int, default=5000, help="Porto TCP de controlo")
    parser.add_argument("--bootstrap-ip", required=True, help="IP do Bootstrapper")
    parser.add_argument("--bootstrap-port", type=int, default=9000, help="Porto do Bootstrapper")
    parser.add_argument("--neighbors", nargs="+", default=None, help="Lista de nós autorizados como vizinhos diretos (ex: --neighbors O1 O2)")
    args = parser.parse_args()

    onode = ONode(args.id, args.port, args.bootstrap_ip, args.bootstrap_port, args.neighbors)
    onode.start()