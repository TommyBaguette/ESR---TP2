import threading
import time
from typing import Dict, Set, Optional, Tuple

class RouteEntry:
    def __init__(self, flow_id: str, cost: int, parent: Optional[str] = None):
        self.flow_id = flow_id
        self.cost = cost
        self.parent = parent
        self.children: Set[str] = set()       # Vizinhos a jusante subscritos
        self.is_active = False                # Se o fluxo está ativo nesta árvore
        self.last_updated = time.time()


class RoutingTable:
    def __init__(self, node_id: str):
        self.node_id = node_id
        # {flow_id: RouteEntry}
        self.routes: Dict[str, RouteEntry] = {}
        # Streams originados localmente caso o nó seja Servidor: {flow_id: cost 0}
        self.local_streams: Set[str] = set()
        self.lock = threading.Lock()

    def register_local_stream(self, flow_id: str):
        """Regista um vídeo originado neste nó (ex: Servidor S1 com custo 0)."""
        with self.lock:
            self.local_streams.add(flow_id)
            entry = RouteEntry(flow_id=flow_id, cost=0, parent=None)
            entry.is_active = True
            self.routes[flow_id] = entry
            print(f"[{self.node_id}] Stream local registado: {flow_id} (Custo 0)")

    def update_route_from_neighbor(self, neighbor_id: str, flow_id: str, advertised_cost: int, link_cost: int = 1) -> bool:
        """
        Recebe a rota anunciada por um vizinho direto.
        Calcula: novo_custo = advertised_cost + link_cost.
        Retorna True se a nossa tabela de rotas melhorou/alterou (para disparar update).
        """
        if flow_id in self.local_streams:
            return False  # Nós somos a fonte original deste stream

        total_cost = advertised_cost + link_cost
        changed = False

        with self.lock:
            if flow_id not in self.routes:
                # Nova rota descoberta
                self.routes[flow_id] = RouteEntry(flow_id=flow_id, cost=total_cost, parent=neighbor_id)
                changed = True
                print(f"[{self.node_id}] [ROTA NOVA] Stream {flow_id} via {neighbor_id} (Custo: {total_cost})")
            else:
                current = self.routes[flow_id]
                # Se vier do mesmo pai e o custo mudou, atualiza
                if current.parent == neighbor_id:
                    if current.cost != total_cost:
                        current.cost = total_cost
                        current.last_updated = time.time()
                        changed = True
                # Se for por outro vizinho e o custo for estritamente menor, comuta o pai
                elif total_cost < current.cost:
                    print(f"[{self.node_id}] [MELHOR ROTA] Stream {flow_id} comuta de {current.parent} para {neighbor_id} ({current.cost} -> {total_cost})")
                    current.parent = neighbor_id
                    current.cost = total_cost
                    current.last_updated = time.time()
                    changed = True

        return changed

    def handle_neighbor_down(self, failed_neighbor: str) -> Set[str]:
        """
        Quando um vizinho cai, invalida as rotas que dependiam dele como parent
        e remove-o de quaisquer listas de children.
        Devolve o conjunto de flow_ids afetados para os quais é preciso procurar alternativa.
        """
        affected_flows = set()
        with self.lock:
            for flow_id, entry in list(self.routes.items()):
                # Se o nó caído era o nosso consumidor (filho), remove-o
                entry.children.discard(failed_neighbor)

                # Se o nó caído era a nossa fonte (pai)
                if entry.parent == failed_neighbor and flow_id not in self.local_streams:
                    entry.parent = None
                    entry.cost = float('inf')
                    entry.is_active = False
                    affected_flows.add(flow_id)
                    print(f"[{self.node_id}] [ROTA PERDIDA] Pai {failed_neighbor} caiu para {flow_id}. Rota invalidada.")

        return affected_flows

    def add_child(self, flow_id: str, child_id: str) -> Tuple[bool, Optional[str]]:
        """
        Processa um JOIN_STREAM recebido:
        - Regista o child_id.
        - Devolve (need_upstream_join, parent_id).
        Se need_upstream_join for False, foi feito GRAFTING num ramo já ativo!
        """
        with self.lock:
            if flow_id not in self.routes:
                return False, None

            entry = self.routes[flow_id]
            entry.children.add(child_id)
            print(f"[{self.node_id}] [MULTICAST] Filho {child_id} adicionado ao fluxo {flow_id}. Total filhos: {len(entry.children)}")

            if not entry.is_active:
                # Este nó ainda não recebia o fluxo; precisa de pedir ao seu próprio pai
                entry.is_active = True
                return True, entry.parent
            else:
                # GRAFTING: este nó já recebe o fluxo; o JOIN para aqui!
                print(f"[{self.node_id}] [GRAFTING] Tronco partilhado para {flow_id}. Ramo ativado sem novo pedido a montante.")
                return False, entry.parent

    def remove_child(self, flow_id: str, child_id: str) -> Tuple[bool, Optional[str]]:
        """
        Processa um LEAVE_STREAM:
        Remove o filho. Se não restarem mais filhos e o nó não for consumidor local,
        desativa o ramo (PRUNING) e devolve (need_upstream_prune, parent_id).
        """
        with self.lock:
            if flow_id not in self.routes:
                return False, None

            entry = self.routes[flow_id]
            entry.children.discard(child_id)
            print(f"[{self.node_id}] [MULTICAST] Filho {child_id} removido de {flow_id}. Restantes: {len(entry.children)}")

            # Se não há mais filhos e não somos a origem, podamos o ramo
            if len(entry.children) == 0 and flow_id not in self.local_streams:
                entry.is_active = False
                print(f"[{self.node_id}] [PRUNING] Sem mais filhos para {flow_id}. Ramo desativado.")
                return True, entry.parent

            return False, None

    def get_exportable_routes(self) -> Dict[str, int]:
        """Devolve um dicionário {flow_id: custo} para partilhar com os vizinhos diretos."""
        with self.lock:
            return {
                flow_id: entry.cost
                for flow_id, entry in self.routes.items()
                if entry.cost != float('inf')
            }