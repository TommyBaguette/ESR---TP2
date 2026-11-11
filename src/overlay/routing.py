import threading
from typing import Dict, Set, Optional, Tuple, NamedTuple

# Margem de histerese: nova rota só é adotada se for pelo menos 15% melhor
HYSTERESIS_FACTOR = 0.85


class RouteUpdateResult(NamedTuple):
    route_changed: bool
    needs_switchover: bool
    old_parent: Optional[str]
    new_parent: Optional[str]


class RouteEntry:
    def __init__(self, flow_id: str, cost: float, parent: Optional[str]):
        self.flow_id = flow_id
        self.cost = cost                # Custo acumulado em RTT (ms)
        self.parent = parent            # Vizinho a montante
        self.children: Set[str] = set() # Vizinhos a jusante subscritos
        self.is_active = False          # True se estiver a receber dados


class RoutingTable:
    def __init__(self, my_id: str):
        self.my_id = my_id
        self.routes: Dict[str, RouteEntry] = {}
        self.lock = threading.Lock()

    def register_local_stream(self, flow_id: str):
        with self.lock:
            entry = RouteEntry(flow_id, cost=0.0, parent=None)
            entry.is_active = True
            self.routes[flow_id] = entry

    def update_route(self, flow_id: str, cost_via_neighbor: float, neighbor_id: str) -> RouteUpdateResult:
        """
        Avalia o vetor de distâncias com métrica de RTT e histerese.
        Gera 'needs_switchover=True' se um fluxo ativo tiver de migrar de pai.
        """
        with self.lock:
            if flow_id not in self.routes:
                self.routes[flow_id] = RouteEntry(flow_id, cost=cost_via_neighbor, parent=neighbor_id)
                return RouteUpdateResult(True, False, None, neighbor_id)

            current = self.routes[flow_id]

            # Fontes de vídeo locais ignoram rotas externas
            if current.cost == 0.0 and current.parent is None:
                return RouteUpdateResult(False, False, None, None)

            # Caso 1: Atualização vinda do pai atual
            if current.parent == neighbor_id:
                if abs(current.cost - cost_via_neighbor) > 0.5:
                    current.cost = cost_via_neighbor
                    return RouteUpdateResult(True, False, None, None)
                return RouteUpdateResult(False, False, None, None)

            # Caso 2: Novo candidato a pai (aplica histerese de 15%)
            if cost_via_neighbor < (current.cost * HYSTERESIS_FACTOR):
                old_parent = current.parent
                current.cost = cost_via_neighbor
                current.parent = neighbor_id

                needs_switch = current.is_active and (old_parent is not None)
                return RouteUpdateResult(True, needs_switch, old_parent, neighbor_id)

            return RouteUpdateResult(False, False, None, None)

    def add_child_to_stream(self, flow_id: str, child_id: str) -> Tuple[bool, Optional[str]]:
        with self.lock:
            entry = self.routes.get(flow_id)
            if not entry:
                return False, None

            entry.children.add(child_id)

            if entry.parent is None:
                return False, None

            if not entry.is_active:
                entry.is_active = True
                return True, entry.parent

            return False, entry.parent

    def remove_child_from_stream(self, flow_id: str, child_id: str) -> Tuple[bool, Optional[str]]:
        with self.lock:
            entry = self.routes.get(flow_id)
            if not entry:
                return False, None

            entry.children.discard(child_id)

            if len(entry.children) > 0:
                return False, None

            if entry.parent is not None and entry.is_active:
                entry.is_active = False
                return True, entry.parent

            return False, None

    def remove_neighbor(self, neighbor_id: str) -> Dict[str, Tuple[bool, Optional[str]]]:
        prune_actions = {}
        with self.lock:
            for fid, entry in list(self.routes.items()):
                if neighbor_id in entry.children:
                    entry.children.discard(neighbor_id)
                    if len(entry.children) == 0 and entry.parent is not None and entry.is_active:
                        entry.is_active = False
                        prune_actions[fid] = (True, entry.parent)

                # Se o pai caiu, a rota fica inacessível de imediato
                if entry.parent == neighbor_id:
                    entry.cost = float("inf")
                    entry.is_active = False
        return prune_actions

    def get_exportable_routes(self) -> Dict[str, float]:
        with self.lock:
            return {
                fid: round(entry.cost, 2)
                for fid, entry in self.routes.items()
                if entry.cost < float("inf")
            }