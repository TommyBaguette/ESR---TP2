import socket
import threading
import argparse
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.common.protocol import send_msg, recv_msg, MSG_BOOTSTRAP_REQ, MSG_BOOTSTRAP_REPLY


class Bootstrapper:
    def __init__(self, host: str = "0.0.0.0", port: int = 9000):
        self.host = host
        self.port = port
        self.known_peers = {}  # {node_id: (ip, control_port)}
        self.lock = threading.Lock()
        self.running = False

    def handle_client(self, client_sock: socket.socket, client_ip: str):
        try:
            msg = recv_msg(client_sock)
            if not msg:
                return

            if msg.get("type") == MSG_BOOTSTRAP_REQ:
                node_id = msg.get("node_id")
                control_port = msg.get("control_port")

                # No método handle_client:
                with self.lock:     
                    # 1. Regista/atualiza o nó antes de devolver a lista        
                    self.known_peers[node_id] = (client_ip, control_port)       
                        
                    # 2. Devolve apenas nós diferentes deste ID e deste par (IP, porto)     
                    peers_to_send = [       
                        [peer_id, ip, port]     
                        for peer_id, (ip, port) in self.known_peers.items()     
                        if peer_id != node_id and not (ip == client_ip and port == control_port)        
                    ]       
                        
                print(f"[BOOTSTRAP] Nó registado: {node_id} ({client_ip}:{control_port}) | Total conhecidos: {len(self.known_peers)}")      

            
                response = {
                    "type": MSG_BOOTSTRAP_REPLY,
                    "peers": peers_to_send
                }
                send_msg(client_sock, response)

        except Exception as e:
            print(f"[BOOTSTRAP] Erro no processamento do cliente {client_ip}: {e}")
        finally:
            client_sock.close()

    def start(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(10)
        self.running = True

        print(f"[BOOTSTRAP] Servidor ativo a escutar em {self.host}:{self.port}")

        try:
            while self.running:
                client_sock, (client_ip, _) = server_sock.accept()
                thread = threading.Thread(
                    target=self.handle_client,
                    args=(client_sock, client_ip),
                    daemon=True
                )
                thread.start()
        except KeyboardInterrupt:
            print("\n[BOOTSTRAP] A encerrar servidor...")
        finally:
            server_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bootstrapper do Serviço OTT")
    parser.add_argument("--host", default="0.0.0.0", help="Endereço IP de escuta")
    parser.add_argument("--port", type=int, default=9000, help="Porto TCP de controlo do bootstrapper")
    args = parser.parse_args()

    bootstrapper = Bootstrapper(host=args.host, port=args.port)
    bootstrapper.start()