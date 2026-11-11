import socket
import threading
import time
import sys
import os
from typing import Dict, Tuple, Optional

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.common.protocol import (
    send_msg, recv_msg,
    MSG_HELLO, MSG_HELLO_ACK, MSG_HELLO_REJECT,
    MSG_PING, MSG_PONG,
    MSG_ROUTING_UPDATE,
    MSG_JOIN_STREAM, MSG_JOIN_ACK, MSG_LEAVE_STREAM
)
from src.overlay.routing import RoutingTable


class NeighborManager:
    def __init__(self, node_id: str, role: str, control_port: int, routing_table: RoutingTable):
        self.node_id = node_id
        self.role = role  # "node" ou "server"
        self.control_port = control_port
        self.routing_table = routing_table

        # {peer_id: {"sock": socket, "addr": (ip, port), "role": str, "rtt": float, "last_seen": float}}
        self.active_neighbors: Dict[str, dict] = {}
        # Conexões ativas por socket para multiplexação
        self.sockets_map: Dict[socket.socket, str] = {}
        self.lock = threading.Lock()
        
        self.running = False
        self.ping_timestamps: Dict[str, float] = {}

    def add_known_contacts(self, peers: list):
        """Tenta ligar-se aos nós conhecidos devolvidos pelo Bootstrapper."""
        for peer in peers:
            peer_id, ip, port = peer[0], peer[1], peer[2]
            if peer_id == self.node_id:
                continue
            with self.lock:
                if peer_id in self.active_neighbors:
                    continue
            threading.Thread(target=self._connect_to_peer, args=(peer_id, ip, port), daemon=True).start()

    def _connect_to_peer(self, peer_id: str, ip: str, port: int):
        """Estabelece ligação de controlo TCP a montante/horizontal."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(4.0)
            sock.connect((ip, port))
            sock.settimeout(None)

            send_msg(sock, {
                "type": MSG_HELLO,
                "node_id": self.node_id,
                "role": self.role,
                "control_port": self.control_port
            })

            reply = recv_msg(sock)
            if reply and reply.get("type") == MSG_HELLO_ACK:
                peer_role = reply.get("role", "node")
                self._register_neighbor(peer_id, sock, (ip, port), peer_role)
                print(f"[{self.node_id}:{self.role}] Vizinhança estabelecida com {peer_id} ({peer_role} em {ip}:{port}).")
                
                # Envia de imediato a nossa tabela de rotas local
                self._send_routing_update_to(peer_id)
                self._listen_to_peer(sock, peer_id)
            else:
                sock.close()
        except Exception:
            pass

    def handle_incoming_connection(self, client_sock: socket.socket, addr: Tuple[str, int]):
        """Processa pedidos de ligação TCP recebidos."""
        try:
            client_sock.settimeout(5.0)
            msg = recv_msg(client_sock)
            client_sock.settimeout(None)

            if not msg or msg.get("type") != MSG_HELLO:
                client_sock.close()
                return

            peer_id = msg.get("node_id")
            peer_role = msg.get("role", "node")
            peer_cport = msg.get("control_port", 0)

            # Regra de isolamento: Servidor nunca atende clientes finais diretamente
            if self.role == "server" and peer_role == "client":
                send_msg(client_sock, {
                    "type": MSG_HELLO_REJECT,
                    "reason": "Servidores apenas atendem nós de infraestrutura (ONodes)."
                })
                client_sock.close()
                return

            send_msg(client_sock, {
                "type": MSG_HELLO_ACK,
                "node_id": self.node_id,
                "role": self.role
            })

            peer_effective_addr = (addr[0], peer_cport)
            self._register_neighbor(peer_id, client_sock, peer_effective_addr, peer_role)
            print(f"[{self.node_id}:{self.role}] Vizinhança aceite com {peer_id} ({peer_role} em {addr[0]}:{peer_cport}).")

            # Partilha de imediato os canais conhecidos
            self._send_routing_update_to(peer_id)
            self._listen_to_peer(client_sock, peer_id)

        except Exception:
            client_sock.close()

    def _register_neighbor(self, peer_id: str, sock: socket.socket, addr: Tuple[str, int], peer_role: str):
        with self.lock:
            self.active_neighbors[peer_id] = {
                "sock": sock,
                "addr": addr,
                "role": peer_role,
                "rtt": 1.0,
                "last_seen": time.time()
            }
            self.sockets_map[sock] = peer_id

    def _listen_to_peer(self, sock: socket.socket, peer_id: str):
        """Loop de escuta dedicado para as mensagens do vizinho."""
        while self.running:
            try:
                msg = recv_msg(sock)
                if not msg:
                    break

                with self.lock:
                    if peer_id in self.active_neighbors:
                        self.active_neighbors[peer_id]["last_seen"] = time.time()

                self._process_message(msg, peer_id, sock)

            except Exception:
                break

        self._unregister_neighbor(peer_id, sock)

    def _process_message(self, msg: dict, peer_id: str, sock: socket.socket):
        m_type = msg.get("type")

        if m_type == MSG_PING:
            send_msg(sock, {"type": MSG_PONG, "node_id": self.node_id})

        elif m_type == MSG_PONG:
            t_send = self.ping_timestamps.get(peer_id)
            if t_send:
                rtt = max(0.1, (time.time() - t_send) * 1000.0)  # RTT em ms
                with self.lock:
                    if peer_id in self.active_neighbors:
                        self.active_neighbors[peer_id]["rtt"] = rtt

        elif m_type == MSG_ROUTING_UPDATE:
            routes = msg.get("routes", {})
            changed = False
            for fid, advertised_cost in routes.items():
                # Custo acumulado (+1 salto ou RTT)
                new_cost = advertised_cost + 1.0
                if self.routing_table.update_route(fid, new_cost, peer_id):
                    changed = True
                    print(f"[{self.node_id}] [ROTA ATUALIZADA] Stream {fid} via {peer_id} (Custo: {new_cost:.1f})")

            if changed:
                self.broadcast_routing_updates()

        elif m_type == MSG_JOIN_STREAM:
            flow_id = msg.get("flow_id")
            requester_id = msg.get("requester_id", peer_id)

            needs_upstream_join, parent_id = self.routing_table.add_child_to_stream(flow_id, requester_id)
            
            with self.routing_table.lock:
                total_children = len(self.routing_table.routes[flow_id].children) if flow_id in self.routing_table.routes else 0
            
            print(f"[{self.node_id}] [MULTICAST] Filho {requester_id} adicionado ao fluxo {flow_id}. Total filhos: {total_children}")

            if needs_upstream_join and parent_id:
                print(f"[{self.node_id}] A propagar JOIN de {flow_id} em direção a {parent_id}...")
                self.send_to_neighbor(parent_id, {
                    "type": MSG_JOIN_STREAM,
                    "flow_id": flow_id,
                    "requester_id": self.node_id
                })
            else:
                print(f"[{self.node_id}] [GRAFTING] Tronco partilhado para {flow_id}. Ramo ativado sem novo pedido a montante.")

            send_msg(sock, {"type": MSG_JOIN_ACK, "flow_id": flow_id})

        elif m_type == MSG_LEAVE_STREAM:
            flow_id = msg.get("flow_id")
            requester_id = msg.get("requester_id", peer_id)

            needs_prune, parent_id = self.routing_table.remove_child_from_stream(flow_id, requester_id)
            
            with self.routing_table.lock:
                rem_children = len(self.routing_table.routes[flow_id].children) if flow_id in self.routing_table.routes else 0
            
            print(f"[{self.node_id}] [MULTICAST] Filho {requester_id} removido de {flow_id}. Restantes: {rem_children}")

            if needs_prune and parent_id:
                print(f"[{self.node_id}] [PRUNING] Sem mais filhos para {flow_id}. Ramo desativado. A propagar LEAVE a {parent_id}...")
                self.send_to_neighbor(parent_id, {
                    "type": MSG_LEAVE_STREAM,
                    "flow_id": flow_id,
                    "requester_id": self.node_id
                })

    def send_to_neighbor(self, peer_id: str, msg: dict) -> bool:
        with self.lock:
            peer_info = self.active_neighbors.get(peer_id)
            if not peer_info:
                return False
            sock = peer_info["sock"]
        return send_msg(sock, msg)

    def _send_routing_update_to(self, peer_id: str):
        routes = self.routing_table.get_exportable_routes()
        self.send_to_neighbor(peer_id, {
            "type": MSG_ROUTING_UPDATE,
            "routes": routes
        })

    def broadcast_routing_updates(self):
        routes = self.routing_table.get_exportable_routes()
        with self.lock:
            recipients = list(self.active_neighbors.values())

        for peer in recipients:
            try:
                send_msg(peer["sock"], {
                    "type": MSG_ROUTING_UPDATE,
                    "routes": routes
                })
            except Exception:
                pass

    def _unregister_neighbor(self, peer_id: str, sock: socket.socket):
        with self.lock:
            if peer_id in self.active_neighbors:
                del self.active_neighbors[peer_id]
            if sock in self.sockets_map:
                del self.sockets_map[sock]

        try:
            sock.close()
        except Exception:
            pass

        print(f"[{self.node_id}] Vizinho {peer_id} desconectado.")
        
        # Trata podas imediatas caso o vizinho que caiu fosse filho ou pai de árvores
        prune_actions = self.routing_table.remove_neighbor(peer_id)
        for flow_id, (needs_prune, upstream_parent) in prune_actions.items():
            if needs_prune and upstream_parent:
                print(f"[{self.node_id}] [PRUNING] Nó {peer_id} caiu. Sem mais filhos para {flow_id}. A podar em {upstream_parent}...")
                self.send_to_neighbor(upstream_parent, {
                    "type": MSG_LEAVE_STREAM,
                    "flow_id": flow_id,
                    "requester_id": self.node_id
                })

        self.broadcast_routing_updates()

    def heartbeat_and_maintenance_loop(self):
        """Loop periódico: mede RTT, remove nós mortos e mantém anúncios ativos."""
        while self.running:
            time.sleep(2.0)
            now = time.time()
            dead_peers = []

            with self.lock:
                active_list = list(self.active_neighbors.items())

            for pid, pdata in active_list:
                # Se não responder há mais de 10 segundos, considera-se caído
                if now - pdata["last_seen"] > 10.0:
                    dead_peers.append((pid, pdata["sock"]))
                    continue

                # Envio de PING com timestamp para medição do RTT
                self.ping_timestamps[pid] = now
                try:
                    send_msg(pdata["sock"], {"type": MSG_PING})
                except Exception:
                    dead_peers.append((pid, pdata["sock"]))

            for pid, sock in dead_peers:
                self._unregister_neighbor(pid, sock)

            # Anúncio periódico do catálogo/rotas
            self.broadcast_routing_updates()