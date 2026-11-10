import socket
import threading
import time
from typing import Dict, List, Tuple
from src.common.protocol import (
    send_msg, recv_msg,
    MSG_HELLO, MSG_HELLO_ACK, MSG_HELLO_REJECT, MSG_PEER_SUGGEST,
    MSG_PING, MSG_PONG,
    MSG_ROUTING_UPDATE, MSG_JOIN_STREAM, MSG_JOIN_ACK, MSG_LEAVE_STREAM
)
from src.overlay.routing import RoutingTable


class NeighborManager:
    def __init__(self, node_id: str, role: str, control_port: int, routing_table: RoutingTable, target_peers: int = 3, max_peers: int = 5):
        self.node_id = node_id
        self.role = role  # "server", "node" ou "client"
        self.control_port = control_port
        self.routing_table = routing_table
        self.target_peers = 1 if role == "client" else target_peers
        self.max_peers = 1 if role == "client" else max_peers

        self.active_neighbors: Dict[str, dict] = {}
        self.known_contacts: Dict[str, Tuple[str, int]] = {}
        
        self.lock = threading.Lock()
        self.running = False

    def is_peer_allowed(self, remote_role: str) -> bool:
        """Regras de admissão de topologia:
        - client só aceita node (nunca liga a server nem a outro client)
        - server só aceita node (nunca liga a client nem a outro server)
        - node aceita toda a gente (server, node, client)
        """
        if self.role == "client":
            return remote_role == "node"
        if self.role == "server":
            return remote_role == "node"
        if self.role == "node":
            return True
        return False

    def add_known_contacts(self, peers_list: List[list]):
        with self.lock:
            for peer in peers_list:
                p_id, p_ip, p_port = peer[0], peer[1], int(peer[2])
                if p_id != self.node_id and p_id not in self.active_neighbors:
                    self.known_contacts[p_id] = (p_ip, p_port)

    def handle_incoming_connection(self, sock: socket.socket, addr: Tuple[str, int]):
        try:
            msg = recv_msg(sock)
            if not msg or msg.get("type") != MSG_HELLO:
                sock.close()
                return

            peer_id = msg.get("node_id")
            peer_role = msg.get("role", "node")
            peer_port = msg.get("control_port")

            if peer_id == self.node_id:
                sock.close()
                return

            # Validação de Papel
            if not self.is_peer_allowed(peer_role):
                send_msg(sock, {
                    "type": MSG_HELLO_REJECT,
                    "reason": f"Incompatibilidade de papéis ({self.role} não admite {peer_role})"
                })
                sock.close()
                return

            with self.lock:
                if peer_id in self.active_neighbors:
                    sock.close()
                    return

                if len(self.active_neighbors) >= self.max_peers:
                    suggested = [[pid, ip, port] for pid, (ip, port) in self.known_contacts.items() if pid != peer_id]
                    send_msg(sock, {
                        "type": MSG_PEER_SUGGEST,
                        "suggested_peers": suggested
                    })
                    sock.close()
                    return

                self.active_neighbors[peer_id] = {
                    "sock": sock,
                    "addr": (addr[0], peer_port),
                    "role": peer_role,
                    "last_seen": time.time()
                }
                self.known_contacts.pop(peer_id, None)

            send_msg(sock, {
                "type": MSG_HELLO_ACK,
                "node_id": self.node_id,
                "role": self.role,
                "status": "ACCEPTED"
            })
            print(f"[{self.node_id}:{self.role}] Vizinhança estabelecida com {peer_id} ({peer_role} em {addr[0]}:{peer_port}).")

            self.send_routing_update_to(peer_id)
            threading.Thread(target=self._listen_neighbor, args=(peer_id, sock), daemon=True).start()

        except Exception:
            sock.close()

    def connect_to_peer(self, peer_id: str, ip: str, port: int) -> bool:
        if peer_id == self.node_id:
            return False

        with self.lock:
            if len(self.active_neighbors) >= self.max_peers or peer_id in self.active_neighbors:
                return False

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect((ip, port))
            sock.settimeout(None)

            send_msg(sock, {
                "type": MSG_HELLO,
                "node_id": self.node_id,
                "role": self.role,
                "control_port": self.control_port
            })

            reply = recv_msg(sock)
            if not reply:
                sock.close()
                return False

            if reply.get("type") == MSG_HELLO_ACK:
                peer_role = reply.get("role", "node")
                # Confirma se o nó remoto também é aceitável para nós
                if not self.is_peer_allowed(peer_role):
                    sock.close()
                    return False

                with self.lock:
                    self.active_neighbors[peer_id] = {
                        "sock": sock,
                        "addr": (ip, port),
                        "role": peer_role,
                        "last_seen": time.time()
                    }
                    self.known_contacts.pop(peer_id, None)

                print(f"[{self.node_id}:{self.role}] Ligação aceite ao vizinho {peer_id} ({peer_role} em {ip}:{port}).")
                self.send_routing_update_to(peer_id)
                threading.Thread(target=self._listen_neighbor, args=(peer_id, sock), daemon=True).start()
                return True

            elif reply.get("type") == MSG_HELLO_REJECT:
                # O nó remoto rejeitou-nos legitimamente por política de papel
                sock.close()
                return False

            elif reply.get("type") == MSG_PEER_SUGGEST:
                self.add_known_contacts(reply.get("suggested_peers", []))
                sock.close()
                return False

        except Exception:
            return False

        return False

    def send_routing_update_to(self, peer_id: str):
        routes = self.routing_table.get_exportable_routes()
        if not routes:
            return
        with self.lock:
            if peer_id in self.active_neighbors:
                try:
                    send_msg(self.active_neighbors[peer_id]["sock"], {
                        "type": MSG_ROUTING_UPDATE,
                        "sender_id": self.node_id,
                        "routes": routes
                    })
                except Exception:
                    pass

    def broadcast_routing_update(self):
        routes = self.routing_table.get_exportable_routes()
        if not routes:
            return
        with self.lock:
            for peer_id, info in list(self.active_neighbors.items()):
                try:
                    send_msg(info["sock"], {
                        "type": MSG_ROUTING_UPDATE,
                        "sender_id": self.node_id,
                        "routes": routes
                    })
                except Exception:
                    pass

    def send_join(self, flow_id: str) -> bool:
        with self.routing_table.lock:
            entry = self.routing_table.routes.get(flow_id)
            if not entry or not entry.parent:
                print(f"[{self.node_id}] Erro: Não há rota conhecida para {flow_id}.")
                return False
            parent_id = entry.parent

        print(f"[{self.node_id}] A enviar JOIN_STREAM de {flow_id} para o pai {parent_id}...")
        return self._send_control_msg(parent_id, {
            "type": MSG_JOIN_STREAM,
            "flow_id": flow_id,
            "requester_id": self.node_id
        })

    def send_leave(self, flow_id: str) -> bool:
        with self.routing_table.lock:
            entry = self.routing_table.routes.get(flow_id)
            if not entry or not entry.parent:
                return False
            parent_id = entry.parent

        return self._send_control_msg(parent_id, {
            "type": MSG_LEAVE_STREAM,
            "flow_id": flow_id,
            "requester_id": self.node_id
        })

    def _send_control_msg(self, target_id: str, msg: dict) -> bool:
        with self.lock:
            if target_id in self.active_neighbors:
                try:
                    send_msg(self.active_neighbors[target_id]["sock"], msg)
                    return True
                except Exception:
                    return False
        return False

    def _listen_neighbor(self, peer_id: str, sock: socket.socket):
        while self.running:
            try:
                msg = recv_msg(sock)
                if not msg:
                    break

                m_type = msg.get("type")

                if m_type == MSG_PING:
                    send_msg(sock, {"type": MSG_PONG, "node_id": self.node_id})
                    self._touch_neighbor(peer_id)

                elif m_type == MSG_PONG:
                    self._touch_neighbor(peer_id)

                elif m_type == MSG_ROUTING_UPDATE:
                    self._touch_neighbor(peer_id)
                    routes = msg.get("routes", {})
                    any_change = False
                    for flow_id, cost in routes.items():
                        if self.routing_table.update_route_from_neighbor(peer_id, flow_id, cost):
                            any_change = True
                    if any_change:
                        self.broadcast_routing_update()

                elif m_type == MSG_JOIN_STREAM:
                    self._touch_neighbor(peer_id)
                    flow_id = msg.get("flow_id")
                    requester = msg.get("requester_id")
                    need_upstream, parent = self.routing_table.add_child(flow_id, requester)

                    send_msg(sock, {
                        "type": MSG_JOIN_ACK,
                        "flow_id": flow_id,
                        "status": "ACCEPTED"
                    })

                    if need_upstream and parent:
                        print(f"[{self.node_id}] A propagar JOIN de {flow_id} em direção a {parent}...")
                        self._send_control_msg(parent, {
                            "type": MSG_JOIN_STREAM,
                            "flow_id": flow_id,
                            "requester_id": self.node_id
                        })

                elif m_type == MSG_LEAVE_STREAM:
                    self._touch_neighbor(peer_id)
                    flow_id = msg.get("flow_id")
                    requester = msg.get("requester_id")
                    need_prune, parent = self.routing_table.remove_child(flow_id, requester)
                    if need_prune and parent:
                        self._send_control_msg(parent, {
                            "type": MSG_LEAVE_STREAM,
                            "flow_id": flow_id,
                            "requester_id": self.node_id
                        })

                elif m_type == MSG_JOIN_ACK:
                    self._touch_neighbor(peer_id)
                    print(f"[{self.node_id}] JOIN_ACK recebido para {msg.get('flow_id')}!")

            except Exception:
                break

        self._remove_neighbor(peer_id)

    def _touch_neighbor(self, peer_id: str):
        with self.lock:
            if peer_id in self.active_neighbors:
                self.active_neighbors[peer_id]["last_seen"] = time.time()

    def _remove_neighbor(self, peer_id: str):
        with self.lock:
            if peer_id in self.active_neighbors:
                info = self.active_neighbors.pop(peer_id)
                try:
                    info["sock"].close()
                except Exception:
                    pass
                print(f"[{self.node_id}] Vizinho {peer_id} desconectado.")

        affected = self.routing_table.handle_neighbor_down(peer_id)
        if affected:
            self.broadcast_routing_update()

    def heartbeat_and_maintenance_loop(self):
        last_route_broadcast = 0
        while self.running:
            time.sleep(2.0)
            now = time.time()
            to_remove = []

            with self.lock:
                for peer_id, info in list(self.active_neighbors.items()):
                    if now - info["last_seen"] > 6.0:
                        to_remove.append(peer_id)
                    else:
                        try:
                            send_msg(info["sock"], {"type": MSG_PING, "node_id": self.node_id})
                        except Exception:
                            to_remove.append(peer_id)

            for peer_id in to_remove:
                self._remove_neighbor(peer_id)

            with self.lock:
                deficit = self.target_peers - len(self.active_neighbors)
                available = list(self.known_contacts.items())

            if deficit > 0 and available:
                for peer_id, (ip, port) in available:
                    if len(self.active_neighbors) >= self.target_peers:
                        break
                    self.connect_to_peer(peer_id, ip, port)

            if now - last_route_broadcast > 10.0:
                self.broadcast_routing_update()
                last_route_broadcast = now