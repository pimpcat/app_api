"""
Errores de negocio del motor de ruteo.

Separados del resto del paquete para que la API y los tests puedan importar
``RuteoError`` sin cargar la lógica SQL completa.
"""


class RuteoError(Exception):
    """Error de negocio del módulo de ruteo (código + mensaje legible)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
