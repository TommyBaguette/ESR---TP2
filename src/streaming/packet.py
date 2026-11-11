import struct
import time
from typing import Tuple, List, Optional

HEADER_FORMAT = "!16sIdHBB"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # Exatamente 32 bytes
MAX_UDP_PAYLOAD = 1400  # Mantém cada datagrama bem abaixo de 1500 bytes


class VideoPacket:
    def __init__(self, flow_id: str, seq_num: int, timestamp: float,
                 frame_num: int, frag_idx: int, frag_total: int, data: bytes):
        self.flow_id = flow_id
        self.seq_num = seq_num
        self.timestamp = timestamp
        self.frame_num = frame_num
        self.frag_idx = frag_idx
        self.frag_total = frag_total
        self.data = data

    def pack(self) -> bytes:
        flow_bytes = self.flow_id.encode('utf-8')[:16].ljust(16, b'\x00')
        header = struct.pack(
            HEADER_FORMAT,
            flow_bytes,
            self.seq_num,
            self.timestamp,
            self.frame_num,
            self.frag_idx,
            self.frag_total
        )
        return header + self.data

    @classmethod
    def unpack(cls, raw_bytes: bytes) -> Optional['VideoPacket']:
        if len(raw_bytes) < HEADER_SIZE:
            return None

        header = raw_bytes[:HEADER_SIZE]
        data = raw_bytes[HEADER_SIZE:]

        flow_bytes, seq_num, timestamp, frame_num, frag_idx, frag_total = struct.unpack(
            HEADER_FORMAT, header
        )
        flow_id = flow_bytes.decode('utf-8', errors='ignore').rstrip('\x00')

        return cls(flow_id, seq_num, timestamp, frame_num, frag_idx, frag_total, data)


def fragment_frame(flow_id: str, frame_num: int, frame_data: bytes,
                   start_seq_num: int) -> Tuple[List[bytes], int]:
    """Divide um frame JPEG completo em pacotes UDP prontos a enviar."""
    max_chunk = MAX_UDP_PAYLOAD - HEADER_SIZE
    chunks = [frame_data[i:i + max_chunk] for i in range(0, len(frame_data), max_chunk)]
    total_frags = len(chunks)
    packets = []
    current_seq = start_seq_num
    now = time.time()

    for idx, chunk in enumerate(chunks):
        pkt = VideoPacket(
            flow_id=flow_id,
            seq_num=current_seq,
            timestamp=now,
            frame_num=frame_num,
            frag_idx=idx,
            frag_total=total_frags,
            data=chunk
        )
        packets.append(pkt.pack())
        current_seq += 1

    return packets, current_seq


class FrameAssembler:
    """Remonta os fragmentos que chegam ao cliente/router até completar o JPEG."""
    def __init__(self):
        # {frame_num: {frag_idx: data}}
        self.buffers = {}
        self.totals = {}
        self.timestamps = {}

    def push_packet(self, pkt: VideoPacket) -> Optional[Tuple[int, bytes, float]]:
        """Devolve (frame_num, jpeg_bytes, timestamp) se o frame ficou completo, senão None."""
        f_num = pkt.frame_num

        if f_num not in self.buffers:
            self.buffers[f_num] = {}
            self.totals[f_num] = pkt.frag_total
            self.timestamps[f_num] = pkt.timestamp

        self.buffers[f_num][pkt.frag_idx] = pkt.data

        # Frame completo?
        if len(self.buffers[f_num]) == self.totals[f_num]:
            ordered_data = bytearray()
            for i in range(self.totals[f_num]):
                ordered_data.extend(self.buffers[f_num][i])

            ts = self.timestamps.pop(f_num)
            self.buffers.pop(f_num)
            self.totals.pop(f_num)

            # Limpeza preventiva de frames órfãos muito antigos (> 5 frames para trás)
            old_keys = [k for k in self.buffers.keys() if k < f_num - 5]
            for k in old_keys:
                self.buffers.pop(k, None)
                self.totals.pop(k, None)
                self.timestamps.pop(k, None)

            return f_num, bytes(ordered_data), ts

        return None