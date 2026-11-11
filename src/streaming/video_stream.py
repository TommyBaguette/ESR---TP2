import os
from typing import Optional


class VideoStream:
    def __init__(self, filename: str):
        if not os.path.exists(filename):
            raise FileNotFoundError(f"Ficheiro de vídeo não encontrado: {filename}")
        self.filename = filename
        self.file = open(filename, 'rb')
        self.frame_num = 0

    def next_frame(self) -> Optional[bytes]:
        """Lê e devolve os bytes do próximo frame JPEG.
        Se chegar ao fim do vídeo, faz loop para o início.
        """
        try:
            # Padrão típico do guião de ESR: os primeiros 5 bytes indicam o tamanho do frame em ASCII
            data = self.file.read(5)
            if data:
                framelength = int(data)
                frame = self.file.read(framelength)
                self.frame_num += 1
                return frame
            else:
                # Fim do vídeo: regressa ao início para manter o stream em loop infinito
                self.file.seek(0)
                data = self.file.read(5)
                if data:
                    framelength = int(data)
                    frame = self.file.read(framelength)
                    self.frame_num = 1
                    return frame
                return None
        except ValueError:
            # Fallback para parsing direto de marcadores JPEG brutos (FF D8 até FF D9)
            return self._read_raw_jpeg()

    def _read_raw_jpeg(self) -> Optional[bytes]:
        buf = bytearray()
        started = False
        while True:
            byte = self.file.read(1)
            if not byte:
                self.file.seek(0)
                if not started:
                    return None
                break
            buf.extend(byte)
            if not started and buf.endswith(b'\xff\xd8'):
                buf = bytearray(b'\xff\xd8')
                started = True
            elif started and buf.endswith(b'\xff\xd9'):
                self.frame_num += 1
                return bytes(buf)
        return None

    def close(self):
        if self.file and not self.file.closed:
            self.file.close()