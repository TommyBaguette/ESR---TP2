import socket
import sys
import os

# Adiciona a raiz do projeto ao path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.common.protocol import send_msg, recv_msg, MSG_BOOTSTRAP_REQ

def testar():
    # 1. Cria socket TCP e liga ao bootstrapper
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("10.0.19.20", 9000))

    # 2. Envia pedido de adesão
    pedido = {
        "type": MSG_BOOTSTRAP_REQ,
        "node_id": "O1",
        "control_port": 5000
    }
    send_msg(sock, pedido)
    print(f"Enviado pedido do nó O1...")

    # 3. Recebe e mostra a resposta
    resposta = recv_msg(sock)
    print(f"Resposta recebida do Bootstrapper: {resposta}")

    sock.close()

if __name__ == "__main__":
    testar()