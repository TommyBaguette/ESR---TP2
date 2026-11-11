import threading
from typing import Dict, Set, Optional, Tuple


class RouteEntry:
    def __init__(self, flow_id: str, cost: float, parent: Optional[str]):
        self.flow_id = flow_id
        self.cost = cost
        self.parent = parent          # Nó a montante (de quem recebemos os dados)
        self.children: Set[str] = set() # Nós a jusante que subscreveram (clientes ou outros ONodes)
        self.is_active = False        # True apenas se a árvore estiver montada e a receber dados


class RoutingTable:
    def __init__(self, my_id: str):
        self.my_id = my_id
        self.routes: Dict[str, RouteEntry] = {}
        self.lock = threading.Lock()

    def register_local_stream(self, flow_id: str):
        """Usado pelo Servidor para declarar os seus fluxos de origem com custo 0."""
        with self.lock:
            entry = RouteEntry(flow_id, cost=0.0, parent=None)
            entry.is_active = True  # O servidor é a fonte permanente
            self.routes[flow_id] = entry

    def update_route(self, flow_id: str, cost_via_neighbor: float, neighbor_id: str) -> bool:
        """
        Atualiza o vetor de distâncias com a métrica recebida de um vizinho.
        Devolve True se a rota mudou (custo menor ou nova rota).
        """
        with self.lock:
            if flow_id not in self.routes:
                self.routes[flow_id] = RouteEntry(flow_id, cost=cost_via_neighbor, parent=neighbor_id)
                return True

            current = self.routes[flow_id]
            # Se formos a fonte local, ignoramos anúncios externos
            if current.cost == 0.0 and current.parent is None:
                return False

            # Se o mesmo pai anuncia um novo custo, ou se encontramos um pai com menor custo
            if current.parent == neighbor_id:
                if current.cost != cost_via_neighbor:
                    current.cost = cost_via_neighbor
                    return True
            elif cost_via_neighbor < current.cost:
                current.cost = cost_via_neighbor
                current.parent = neighbor_id
                return True

            return False

    def add_child_to_stream(self, flow_id: str, child_id: str) -> Tuple[bool, Optional[str]]:
        """
        Regista um novo filho para o stream.
        Devolve: (needs_upstream_join, parent_id)
        - needs_upstream_join é True se este nó NÃO estava a receber o stream e precisa de pedir ao pai.
        - needs_upstream_join é False se já estava ativo (Grafting local).
        """
        with self.lock:
            entry = self.routes.get(flow_id)
            if not entry:
                return False, None

            entry.children.add(child_id)

            # Se for o próprio servidor de origem, não precisa de pedir nada a montante
            if entry.parent is None:
                return False, None

            # Se a rota estava inativa (ramo desligado/podado), agora tem de ser ativada
            if not entry.is_active:
                entry.is_active = True
                return True, entry.parent

            # Se já estava ativo, o tronco é partilhado (Grafting)
            return False, entry.parent

    def remove_child_from_stream(self, flow_id: str, child_id: str) -> Tuple[bool, Optional[str]]:
        """
        Remove um filho da árvore de distribuição.
        Devolve: (needs_upstream_prune, parent_id)
        - needs_upstream_prune é True quando já não sobra nenhum filho e o ramo deve ser podado.
        """
        with self.lock:
            entry = self.routes.get(flow_id)
            if not entry:
                return False, None

            entry.children.discard(child_id)

            # Se ainda existem outros filhos a assistir, continua ativo
            if len(entry.children) > 0:
                return False, None

            # Se não restam filhos e tínhamos um pai a montante:
            if entry.parent is not None and entry.is_active:
                entry.is_active = False  # <--- REPOSIÇÃO OBRIGATÓRIA A FALSE
                return True, entry.parent

            return False, None

    def remove_neighbor(self, neighbor_id: str) -> Dict[str, Tuple[bool, Optional[str]]]:
        """
        Trata da queda abrupta de um vizinho (cliente ou ONode pai/filho).
        Limpa-o como filho de todas as rotas e aciona poda se necessário.
        """
        prune_actions = {}
        with self.lock:
            for fid, entry in list(self.routes.items()):
                if neighbor_id in entry.children:
                    entry.children.discard(neighbor_id)
                    if len(entry.children) == 0 and entry.parent is not None and entry.is_active:
                        entry.is_active = False
                        prune_actions[fid] = (True, entry.parent)

                # Se o vizinho que caiu era o nosso pai, a rota fica inválida
                if entry.parent == neighbor_id:
                    entry.cost = float("inf")
                    entry.is_active = False
        return prune_actions

    def get_exportable_routes(self) -> Dict[str, float]:
        """Devolve o dicionário {flow_id: custo} para enviar aos vizinhos nos ROUTING_UPDATE."""
        with self.lock:
            return {
                fid: entry.cost
                for fid, entry in self.routes.items()
                if entry.cost < float("inf")
            }