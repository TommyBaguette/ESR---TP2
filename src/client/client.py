import socket
import threading
import argparse
import sys
import os
import time
import io

# Interface Gráfica / Renderização de Imagem
try:
    import tkinter as tk
    from PIL import Image, ImageTk
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.common.protocol import (
    send_msg, recv_msg,
    MSG_HELLO, MSG_HELLO_ACK, MSG_HELLO_REJECT,
    MSG_PING, MSG_PONG,
    MSG_ROUTING_UPDATE, MSG_JOIN_STREAM, MSG_JOIN_ACK, MSG_LEAVE_STREAM
)
from src.streaming.packet import VideoPacket, FrameAssembler


class StreamClient:
    def __init__(self, client_id: str, access_node_ip: str, access_node_port: int, data_port: int = 5001):
        self.client_id = client_id
        self.access_node_ip = access_node_ip
        self.access_node_port = access_node_port
        self.data_port = data_port
        
        self.tcp_sock = None
        self.udp_sock = None
        self.running = False
        
        self.known_streams = {}
        self.active_stream = None
        
        self.assembler = FrameAssembler()
        self.frames_received = 0
        self.last_frame_bytes = None
        
        # Elementos GUI (Tkinter)
        self.root = None
        self.img_label = None

    def connect(self) -> bool:
        print(f"[{self.client_id}] A ligar ao ponto de acesso local ({self.access_node_ip}:{self.access_node_port})...")
        try:
            self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_sock.settimeout(4.0)
            self.tcp_sock.connect((self.access_node_ip, self.access_node_port))
            self.tcp_sock.settimeout(None)

            send_msg(self.tcp_sock, {
                "type": MSG_HELLO,
                "node_id": self.client_id,
                "role": "client",
                "control_port": 0  # Cliente não atua como servidor TCP
            })

            reply = recv_msg(self.tcp_sock)
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
        if not self.tcp_sock:
            print(f"[{self.client_id}] Erro: Sem ligação ativa.")
            return

        print(f"[{self.client_id}] A subscrever stream {stream_id}...")
        send_msg(self.tcp_sock, {
            "type": MSG_JOIN_STREAM,
            "flow_id": stream_id,
            "requester_id": self.client_id
        })
        self.active_stream = stream_id
        self.frames_received = 0

    def leave(self):
        if not self.tcp_sock or not self.active_stream:
            return

        print(f"[{self.client_id}] A cancelar subscrição de {self.active_stream}...")
        send_msg(self.tcp_sock, {
            "type": MSG_LEAVE_STREAM,
            "flow_id": self.active_stream,
            "requester_id": self.client_id
        })
        self.active_stream = None

    def _control_listen_loop(self):
        while self.running:
            try:
                msg = recv_msg(self.tcp_sock)
                if not msg:
                    print(f"\n[{self.client_id}] Desconectado do nó de acesso.")
                    break

                m_type = msg.get("type")
                if m_type == MSG_PING:
                    send_msg(self.tcp_sock, {"type": MSG_PONG, "node_id": self.client_id})

                elif m_type == MSG_ROUTING_UPDATE:
                    routes = msg.get("routes", {})
                    if set(routes.keys()) != set(self.known_streams.keys()):
                        self.known_streams = routes
                        print(f"\n[{self.client_id}] Catálogo atualizado: {list(routes.keys())}")
                    else:
                        self.known_streams = routes

                elif m_type == MSG_JOIN_ACK:
                    print(f"\n[{self.client_id}] Stream {msg.get('flow_id')} ativo! A aguardar pacotes UDP...")

            except Exception:
                break
        self.running = False

    def _udp_receive_loop(self):
        """Escuta datagramas UDP na porta de dados e remonta os frames JPEG."""
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sock.bind(("0.0.0.0", self.data_port))
        print(f"[{self.client_id}] Recetor de vídeo UDP ativo no porto {self.data_port}")

        while self.running:
            try:
                data, _ = self.udp_sock.recvfrom(65535)
                pkt = VideoPacket.unpack(data)
                if not pkt:
                    continue

                if self.active_stream and pkt.flow_id == self.active_stream:
                    result = self.assembler.push_packet(pkt)
                    if result:
                        frame_num, jpeg_data, timestamp = result
                        self.frames_received += 1
                        self.last_frame_bytes = jpeg_data

                        if not HAS_GUI and self.frames_received % 25 == 0:
                            latency_ms = (time.time() - timestamp) * 1000
                            print(f"[{self.client_id}] A reproduzir frame #{frame_num} ({len(jpeg_data)} B, latência est.: {latency_ms:.1f}ms)")
            except Exception:
                break

    def _gui_update_loop(self):
        """Atualiza a janela Tkinter a ~30 FPS com o último frame JPEG recebido."""
        if not HAS_GUI or not self.root:
            return

        if self.last_frame_bytes:
            try:
                image = Image.open(io.BytesIO(self.last_frame_bytes))
                tk_img = ImageTk.PhotoImage(image)
                self.img_label.config(image=tk_img)
                self.img_label.image = tk_img
            except Exception:
                pass

        if self.running:
            self.root.after(33, self._gui_update_loop)

    def interactive_cli(self):
        time.sleep(0.5)
        print(f"\n[{self.client_id}] Comandos do leitor:")
        print("  list          -> Ver canais disponíveis")
        print("  join <stream> -> Iniciar visualização")
        print("  leave         -> Parar visualização\n")

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

    def start(self):
        if not self.connect():
            return

        self.running = True
        threading.Thread(target=self._control_listen_loop, daemon=True).start()
        threading.Thread(target=self._udp_receive_loop, daemon=True).start()
        threading.Thread(target=self.interactive_cli, daemon=True).start()

        # Se tiver interface gráfica disponível (Display X11 ativo no CORE), abre a janela do vídeo
        if HAS_GUI and os.environ.get("DISPLAY"):
            try:
                self.root = tk.Tk()
                self.root.title(f"Reprodutor OTT - {self.client_id}")
                self.root.geometry("400x320")
                self.img_label = tk.Label(self.root, text=f"À espera de subscrição no nó {self.client_id}...")
                self.img_label.pack(expand=True, fill="both")
                
                self.root.after(33, self._gui_update_loop)
                self.root.mainloop()
            except Exception as e:
                print(f"[{self.client_id}] Aviso: GUI indisponível ({e}). A operar em modo consola.")
        else:
            print(f"[{self.client_id}] Sem servidor gráfico (DISPLAY). A operar em modo consola.")
            while self.running:
                time.sleep(1)

        self.leave()
        if self.tcp_sock:
            self.tcp_sock.close()
        if self.udp_sock:
            self.udp_sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente Leitor de Streaming OTT")
    parser.add_argument("--id", required=True, help="Identificador do cliente (ex: C1)")
    parser.add_argument("--access-ip", required=True, help="IP do ONode local")
    parser.add_argument("--access-port", type=int, default=5000, help="Porto TCP do ONode")
    parser.add_argument("--data-port", type=int, default=5001, help="Porto UDP de escuta do cliente")
    args = parser.parse_args()

    client = StreamClient(args.id, args.access_ip, args.access_port, args.data_port)
    client.start()