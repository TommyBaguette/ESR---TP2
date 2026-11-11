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
from src.streaming.packet import fragment_frame
from src.streaming.video_stream import VideoStream


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
    def __init__(self, server_id: str, control_port: int, bootstrap_ip: str, bootstrap_port: int, stream_mappings: dict):
        self.server_id = server_id
        self.control_port = control_port
        self.data_port = control_port + 1
        self.local_ip = get_local_ip()
        self.bootstrap_ip = bootstrap_ip
        self.bootstrap_port = bootstrap_port

        self.stream_mappings = stream_mappings  # {flow_id: path_to_file}
        self.routing_table = RoutingTable(self.server_id)
        self.neighbor_mgr = NeighborManager(self.server_id, "server", self.control_port, self.routing_table)
        
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = False

        # Registo de todos os fluxos com Custo 0
        for s in self.stream_mappings.keys():
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
                print(f"[{self.server_id}:Server] Contactos obtidos do Bootstrap: {len(peers)}")
                self.neighbor_mgr.add_known_contacts(peers)
        except Exception as e:
            print(f"[{self.server_id}:Server] Aviso: Bootstrapper inacessível ({e})")

    def _stream_worker(self, flow_id: str, filepath: str):
        """Thread responsável por debitar frames do fluxo a uma taxa estável (~25 FPS)."""
        print(f"[{self.server_id}:Server] Worker de vídeo pronto para {flow_id} ({filepath}).")
        
        try:
            reader = VideoStream(filepath)
        except Exception as e:
            print(f"[{self.server_id}:Server] Erro ao abrir vídeo '{filepath}': {e}")
            return

        seq_num = 0
        frame_interval = 1.0 / 25.0  # 40 ms por frame

        while self.running:
            start_t = time.time()

            # Obter a lista de filhos ativos na árvore multicast
            dest_addrs = []
            with self.routing_table.lock:
                entry = self.routing_table.routes.get(flow_id)
                if entry and entry.children:
                    with self.neighbor_mgr.lock:
                        for child_id in entry.children:
                            if child_id in self.neighbor_mgr.active_neighbors:
                                child_ip, child_cport = self.neighbor_mgr.active_neighbors[child_id]["addr"]
                                child_dport = child_cport + 1  # Porto UDP do filho
                                dest_addrs.append((child_ip, child_dport))

            # Se ninguém estiver a assistir, pausa a leitura para não consumir CPU nem saltar frames
            if not dest_addrs:
                time.sleep(0.1)
                continue

            frame_data = reader.next_frame()
            if not frame_data:
                time.sleep(0.05)
                continue

            # Fragmentar frame em pacotes UDP
            packets, seq_num = fragment_frame(flow_id, reader.frame_num, frame_data, seq_num)

            # Transmissão Multicast lógica: enviar cada pacote UDP aos filhos ativos
            for pkt_bytes in packets:
                for addr in dest_addrs:
                    try:
                        self.udp_sock.sendto(pkt_bytes, addr)
                    except Exception:
                        pass

            elapsed = time.time() - start_t
            sleep_time = max(0.0, frame_interval - elapsed)
            time.sleep(sleep_time)

        reader.close()

    def interactive_cli(self):
        time.sleep(1.0)
        print(f"\n[{self.server_id}:Server] Comandos:")
        print("  status -> Ver streams e destinos ativos\n")

        while self.running:
            try:
                cmd = input().strip().split()
                if not cmd:
                    continue
                if cmd[0] == "status":
                    print(f"--- Origens de Vídeo em {self.server_id} ---")
                    with self.routing_table.lock:
                        for fid, r in self.routing_table.routes.items():
                            print(f"  Stream: {fid} | Destinatários (filhos): {list(r.children)}")
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
        print(f"[{self.server_id}:Server] Controlo TCP em {self.local_ip}:{self.control_port} | Dados UDP em :{self.data_port}")

        self.perform_bootstrap()

        # Iniciar threads de envio de vídeo para cada stream registado
        for flow_id, filepath in self.stream_mappings.items():
            threading.Thread(target=self._stream_worker, args=(flow_id, filepath), daemon=True).start()

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
            self.udp_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor de Streaming OTT com UDP")
    parser.add_argument("--id", required=True, help="Identificador do servidor (ex: S1)")
    parser.add_argument("--port", type=int, default=5000, help="Porto TCP de controlo")
    parser.add_argument("--bootstrap-ip", required=True, help="IP do Bootstrapper")
    parser.add_argument("--bootstrap-port", type=int, default=9000, help="Porto do Bootstrapper")
    parser.add_argument(
        "--stream", nargs="+", required=True,
        help="Mapeamento de streams no formato ID=ficheiro (ex: S1:A=movie.Mjpeg)"
    )
    args = parser.parse_args()

    mappings = {}
    for item in args.stream:
        if "=" in item:
            fid, path = item.split("=", 1)
            mappings[fid] = path
        else:
            # Fallback caso não seja passado ficheiro: tenta ler 'movie.Mjpeg' local
            mappings[item] = "movie.Mjpeg"

    server = VideoServer(args.id, args.port, args.bootstrap_ip, args.bootstrap_port, mappings)
    server.start()