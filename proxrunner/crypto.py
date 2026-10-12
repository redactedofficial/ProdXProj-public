"""Password-protected streaming AES-256-GCM envelope; keys never enter the envelope."""
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"PROXENC1"
BLOCK = 1024 * 1024


def creation_password(password):
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("New encrypted packages/backups require a password of at least 12 characters")
    return password


def key(password, salt):
    if not password:
        raise ValueError("An encryption password is required")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def encrypt(source, destination, password):
    creation_password(password)
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(key(password, salt)), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    with open(source, "rb") as inp, open(destination, "wb") as out:
        out.write(header)
        while chunk := inp.read(BLOCK):
            out.write(cipher.update(chunk))
        out.write(cipher.finalize())
        out.write(cipher.tag)


def decrypt(source, destination, password):
    with open(source, "rb") as inp:
        header = inp.read(36)
        if len(header) != 36 or header[:8] != MAGIC:
            raise ValueError("Invalid encrypted package")
        size = os.fstat(inp.fileno()).st_size
        if size < 52:
            raise ValueError("Truncated encrypted package")
        inp.seek(-16, 2)
        tag = inp.read(16)
        inp.seek(36)
        cipher = Cipher(algorithms.AES(key(password, header[8:24])),
                        modes.GCM(header[24:36], tag)).decryptor()
        cipher.authenticate_additional_data(header)
        remaining = size - 52
        try:
            with open(destination, "wb") as out:
                while remaining:
                    chunk = inp.read(min(BLOCK, remaining))
                    if not chunk:
                        raise ValueError("Truncated encrypted package")
                    out.write(cipher.update(chunk))
                    remaining -= len(chunk)
                out.write(cipher.finalize())
        except InvalidTag:
            raise ValueError("Wrong password or damaged package") from None
