import socket
import threading
import argparse
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.common.protocol import send_msg, recv_msg, MSG_BOOTSTRAP_REQ, MSG_BOOTSTRAP_REPLY


class Bootstrapper:
    def __init__(self, port: int):
        self.port = port
        # Armazena os nós de infraestrutura: {node_id: (ip, control_port)}
        self.registered_nodes = {}
        self.lock = threading.Lock()
        self.running = False

    def handle_client(self, client_sock: socket.socket, addr: tuple):
        try:
            msg = recv_msg(client_sock)
            if not msg or msg.get("type") != MSG_BOOTSTRAP_REQ:
                client_sock.close()
                return

            node_id = msg.get("node_id")
            control_port = msg.get("control_port")
            node_ip = addr[0]

            with self.lock:
                # Lista de contactos existentes para devolver (excluindo o próprio)
                peers = [
                    [nid, ip, port]
                    for nid, (ip, port) in self.registered_nodes.items()
                    if nid != node_id
                ]

                # Regista o novo nó na lista de nós conhecidos
                self.registered_nodes[node_id] = (node_ip, control_port)
                print(f"[BOOTSTRAP] Nó registado: {node_id} ({node_ip}:{control_port}) | Total conhecidos: {len(self.registered_nodes)}")

            send_msg(client_sock, {
                "type": MSG_BOOTSTRAP_REPLY,
                "peers": peers
            })

        except Exception as e:
            print(f"[BOOTSTRAP] Erro ao atender nó: {e}")
        finally:
            client_sock.close()

    def start(self):
        self.running = True
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("0.0.0.0", self.port))
        server_sock.listen(20)
        print(f"[BOOTSTRAP] Serviço ativo a escutar em 0.0.0.0:{self.port}")

        try:
            while self.running:
                client_sock, addr = server_sock.accept()
                threading.Thread(target=self.handle_client, args=(client_sock, addr), daemon=True).start()
        except KeyboardInterrupt:
            print("\n[BOOTSTRAP] A encerrar serviço...")
        finally:
            self.running = False
            server_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serviço de Descoberta Inicial (Bootstrapper)")
    parser.add_argument("--port", type=int, default=9000, help="Porto TCP de escuta")
    args = parser.parse_args()

    bootstrapper = Bootstrapper(args.port)
    bootstrapper.start()