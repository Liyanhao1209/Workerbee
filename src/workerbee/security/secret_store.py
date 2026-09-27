"""凭据存取（架构设计 v0.02 §5.5 SecretStore、§9.1、§15）。

首版实现按 §15 的建议：**AES-256-GCM 加密文件 + 用户口令经 Argon2id 派生密钥**，
只用 ``cryptography`` 与 ``argon2-cffi``，不引入新依赖。

文件格式（全部大端定长头 + 变长体）::

    magic(8) version(2) time_cost(4) memory_cost(4) parallelism(4) hash_len(1)
    salt(16) | kcv_nonce(12) kcv_ct(39)     ← 头部校验值，只加密一个固定常量
    body_nonce(12) body_ct(n)               ← 真正的密文

设计要点与理由：

- **AAD 绑定头部。** ``magic|version|params|salt`` 进 AEAD 的附加数据。改动其中
  任何一字节，解密都会失败而不是被静默接受——参数与版本一旦被改，语义就变了，
  不能让它「还能解开」。**诚实的补充**：在 v1 这一版里，改头部必然同时改掉密钥
  派生的输入，所以 AAD 与密钥派生是冗余的两道锁；它真正要防的是**版本演进**
  （例如 v2 换 KDF／换载荷结构时，把 v1 文件的版本字节改成 v2 不应被 v2 读取器
  当成合法输入）以及日后任何「参数外置／密钥缓存」的优化把头部变成不可信输入。
  :func:`_verify_header` 的头部校验值则让这类改动在**碰正文之前**就被拒绝。
- **头部校验值（KCV）。** 用同一把密钥、同一份 AAD 加密一段固定常量。它让
  「口令不对／头部被改」与「正文被改」可以被分开报告：前者在碰正文之前就失败。
  它不额外泄漏任何东西——攻击者本来就能拿密文本体做口令猜测。
- **每次写盘重新生成 nonce。** nonce 由 ``os.urandom`` 生成，且**只在写入时产生**，
  不存在「同一密钥复用同一 nonce」的路径（GCM 下重用 nonce 会直接毁掉保密性）。
- **明文只在内存。** 落盘内容永远是密文；:meth:`SecretStore.lock` 会就地清零
  派生密钥并清空明文表。
- **凭据不进日志、不进异常消息。** 本模块所有异常与 ``repr`` 只出现 locator、
  路径与参数，绝不出现值或口令。

**如实声明的能力边界**（不要把首版当保险箱）：

- 不防已被攻陷的本机：同机进程可以读内存、读键盘、替换本模块本身。
- 不防回滚／重放：没有单调计数器，攻击者拿到旧文件副本就能把库退回去。
- 不防离线爆破：安全性完全取决于口令强度与 Argon2 参数；文件可以被拷走慢慢猜。
- 内存清零是**尽力而为**：CPython 的 ``str`` 不可变、分配器也不保证归零，
  ``lock()`` 保证的是「本对象不再持有明文引用」，不是物理擦除。真正的防内存
  取证应等 §15 二期的 OS keychain 方案。
- 单文件全量重写：每次写入都重新加密整个库。库很小（凭据条数有限），够用；
  但不要拿它存大对象。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import struct
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..core.domain.base import utcnow
from .redaction import DEFAULT_MASK, MIN_LITERAL_LENGTH, OutboundScanner

__all__ = [
    "KdfParams",
    "SecretMeta",
    "VaultFile",
    "SecretStore",
    "SecretRedactor",
    "SecretStoreError",
    "StoreLockedError",
    "StoreNotFoundError",
    "StoreExistsError",
    "StoreFormatError",
    "VaultIntegrityError",
    "PassphraseError",
    "StoreCorruptError",
    "SecretNotFoundError",
    "SecretRevokedError",
    "parse_vault",
    "read_vault",
]


# ---------------------------------------------------------------------------
# 常量：文件格式与 KDF
# ---------------------------------------------------------------------------

MAGIC = b"WBSECRET"
"""8 字节魔数。用于在解密之前就把「这不是本格式文件」区分出来。"""

VAULT_VERSION = 1
SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})

SALT_LEN = 16
NONCE_LEN = 12
"""GCM 标准 nonce 长度。每次写盘重新生成，绝不复用。"""

KEY_LEN = 32
"""AES-256。"""

GCM_TAG_LEN = 16

KCV_PLAINTEXT = b"workerbee-secret-store-keycheck-v1"
"""头部校验值的明文常量：不是秘密，只用来验证「密钥 + 头部」自洽。"""

KCV_CT_LEN = len(KCV_PLAINTEXT) + GCM_TAG_LEN

_PREFIX = struct.Struct(">8sHIIIB")
"""magic(8) | version(2) | time_cost(4) | memory_cost(4) | parallelism(4) | hash_len(1)"""

_AAD_LEN = 8 + 2 + 4 + 4 + 4 + 1 + SALT_LEN  # magic|version|params|salt = 39
_SALT_OFFSET = _AAD_LEN - SALT_LEN  # 23
_KCV_NONCE_OFFSET = _AAD_LEN  # 39
_KCV_CT_OFFSET = _KCV_NONCE_OFFSET + NONCE_LEN  # 51
_BODY_NONCE_OFFSET = _KCV_CT_OFFSET + KCV_CT_LEN  # 90
_BODY_CT_OFFSET = _BODY_NONCE_OFFSET + NONCE_LEN  # 102
_MIN_FILE_LEN = _BODY_CT_OFFSET + GCM_TAG_LEN

DEFAULT_TIME_COST = 3
DEFAULT_MEMORY_COST = 65536
"""64 MiB。单机桌面场景下与 3 次迭代配合，约 0.1–0.3 秒一次派生。"""

DEFAULT_PARALLELISM = 1
"""固定为 1：跨平台结果一致优先于本机多核加速（§15 口径）。"""

MAX_TIME_COST = 64
MAX_MEMORY_COST = 1 << 20  # KiB = 1 GiB
MAX_PARALLELISM = 16
"""参数上界。读取时先做范围校验再派生：否则一个被改成天文数字的 memory_cost
就能让打开动作变成拒绝服务（而且是在我们做任何完整性判断之前）。"""


# ---------------------------------------------------------------------------
# 异常：消息里只允许出现 locator / 路径 / 参数
# ---------------------------------------------------------------------------


class SecretStoreError(RuntimeError):
    """本模块所有错误的基类。"""


class StoreLockedError(SecretStoreError):
    """库未解锁（从未 open，或已被 :meth:`SecretStore.lock` 清空）。"""


class StoreNotFoundError(SecretStoreError):
    """库文件不存在。新库请用 :meth:`SecretStore.create`。"""


class StoreExistsError(SecretStoreError):
    """目标路径已有库文件。create 不覆盖既有库，以免抹掉唯一的凭据副本。"""


class StoreFormatError(SecretStoreError):
    """文件头不合法：魔数不符、版本不支持、KDF 参数越界、文件被截断。"""


class VaultIntegrityError(SecretStoreError):
    """完整性校验失败。绝不返回（也不猜测）任何明文。"""


class PassphraseError(VaultIntegrityError):
    """口令不对——**或**文件头被篡改／损坏。

    AES-GCM 无法区分这两者：两种情况下派生出的密钥都过不了头部校验。异常消息
    如实写明这一点，且只带路径，不带任何口令或密钥材料。
    """


class StoreCorruptError(VaultIntegrityError):
    """头部校验通过（密钥与头部都对），但密文本体校验失败：文件被改动或损坏。"""


class SecretNotFoundError(SecretStoreError):
    """locator 不存在。"""


class SecretRevokedError(SecretStoreError):
    """locator 已撤销：材料已被丢弃，不得再被读取或直接覆盖（§9.1）。"""


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KdfParams:
    """Argon2id 参数。落盘并进 AAD，因此改动会被检测到。"""

    time_cost: int = DEFAULT_TIME_COST
    memory_cost: int = DEFAULT_MEMORY_COST
    parallelism: int = DEFAULT_PARALLELISM
    hash_len: int = KEY_LEN

    def validate(self) -> None:
        """越界即报错。**在派生之前**调用，避免被恶意参数拖成拒绝服务。"""
        if not 1 <= self.time_cost <= MAX_TIME_COST:
            raise StoreFormatError(f"Argon2 time_cost 越界: {self.time_cost}")
        if not 8 <= self.memory_cost <= MAX_MEMORY_COST:
            raise StoreFormatError(f"Argon2 memory_cost 越界: {self.memory_cost}")
        if not 1 <= self.parallelism <= MAX_PARALLELISM:
            raise StoreFormatError(f"Argon2 parallelism 越界: {self.parallelism}")
        if self.hash_len != KEY_LEN:
            raise StoreFormatError(f"不支持的密钥长度: {self.hash_len}")
        if self.memory_cost < 8 * self.parallelism:
            raise StoreFormatError("Argon2 memory_cost 小于 8 × parallelism")
        return None

    def describe(self) -> str:
        return (
            f"argon2id(t={self.time_cost}, m={self.memory_cost}KiB, "
            f"p={self.parallelism}, len={self.hash_len})"
        )


@dataclass(frozen=True, slots=True)
class SecretMeta:
    """一条凭据的**元数据**。刻意不含值——``list_locators()`` 只回这个类型。"""

    locator: str
    label: str
    created_at: str
    updated_at: str
    revoked: bool = False

    def __repr__(self) -> str:  # 显式写出，防止日后有人往这里加值字段
        return (
            f"SecretMeta(locator={self.locator!r}, label={self.label!r}, "
            f"created_at={self.created_at!r}, updated_at={self.updated_at!r}, "
            f"revoked={self.revoked})"
        )


@dataclass(frozen=True, slots=True)
class VaultFile:
    """解析后的库文件结构。**不含任何明文**，可安全用于排查与测试。"""

    version: int
    params: KdfParams
    salt: bytes
    kcv_nonce: bytes
    kcv_ct: bytes
    body_nonce: bytes
    body_ct: bytes

    @property
    def aad(self) -> bytes:
        """AEAD 附加数据 = magic|version|params|salt。"""
        return _pack_aad(self.version, self.params, self.salt)

    def describe(self) -> str:
        return (
            f"VaultFile(v{self.version}, {self.params.describe()}, "
            f"salt={self.salt.hex()[:8]}…, body_ct={len(self.body_ct)}B)"
        )


def _pack_aad(version: int, params: KdfParams, salt: bytes) -> bytes:
    """AAD 的唯一定义处。任何改动都会同时改变写入与校验两侧，不会漂移。"""
    return (
        _PREFIX.pack(
            MAGIC,
            version,
            params.time_cost,
            params.memory_cost,
            params.parallelism,
            params.hash_len,
        )
        + salt
    )


def parse_vault(blob: bytes) -> VaultFile:
    """解析库文件结构。只做结构校验，**不派生密钥、不解密**。"""
    if len(blob) < _MIN_FILE_LEN:
        raise StoreFormatError(
            f"文件长度不足（{len(blob)} < {_MIN_FILE_LEN}B），可能被截断或不是本格式"
        )
    magic, version, time_cost, memory_cost, parallelism, hash_len = _PREFIX.unpack_from(
        blob, 0
    )
    if magic != MAGIC:
        raise StoreFormatError("魔数不符：这不是 Workerbee Secret Store 文件")
    if version not in SUPPORTED_VERSIONS:
        raise StoreFormatError(
            f"不支持的 vault 版本 {version}；本程序支持 {sorted(SUPPORTED_VERSIONS)}"
        )
    params = KdfParams(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=hash_len,
    )
    params.validate()
    return VaultFile(
        version=version,
        params=params,
        salt=blob[_SALT_OFFSET:_AAD_LEN],
        kcv_nonce=blob[_KCV_NONCE_OFFSET:_KCV_CT_OFFSET],
        kcv_ct=blob[_KCV_CT_OFFSET:_BODY_NONCE_OFFSET],
        body_nonce=blob[_BODY_NONCE_OFFSET:_BODY_CT_OFFSET],
        body_ct=blob[_BODY_CT_OFFSET:],
    )


def read_vault(path: str | Path) -> VaultFile:
    """读取并解析库文件头（同步，仅供排查与测试）。不会解密任何内容。"""
    return parse_vault(Path(path).read_bytes())


def _wipe(buf: bytearray) -> None:
    """就地清零一个可写缓冲区。对 ``bytes``／``str`` 无能为力（不可变）。"""
    for i in range(len(buf)):
        buf[i] = 0


def _derive_key(passphrase: str, salt: bytes, params: KdfParams) -> bytearray:
    """Argon2id 派生。返回 ``bytearray`` 以便 :meth:`SecretStore.lock` 能就地清零。"""
    params.validate()
    try:
        raw = hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=params.time_cost,
            memory_cost=params.memory_cost,
            parallelism=params.parallelism,
            hash_len=params.hash_len,
            type=Argon2Type.ID,
        )
    except Exception as exc:  # 参数已在上面校验过，走到这里属意外
        raise SecretStoreError(
            f"密钥派生失败（{params.describe()}）：{type(exc).__name__}"
        ) from None
    key = bytearray(raw)
    return key


def _validate_passphrase(passphrase: Any) -> str:
    if not isinstance(passphrase, str):
        raise TypeError("passphrase 必须是 str")
    if not passphrase:
        raise ValueError("passphrase 不能为空")
    return passphrase


def _default_label(locator: str) -> str:
    """从 locator 推一个可读标签，例如 ``secret://openai`` → ``openai``。"""
    tail = locator.split("://", 1)[-1]
    return tail.rsplit("/", 1)[-1] or locator


def _write_atomic(path: Path, blob: bytes) -> None:
    """原子替换：先写同目录临时文件再 ``os.replace``，避免写一半断电毁掉整个库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".wbsecret-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Secret Redactor
# ---------------------------------------------------------------------------

REDACTION_FAILED = "[已阻止：脱敏失败]"
"""脱敏器内部出错时的占位符。**失败必须关闸**（回吐原文才是事故）。"""


class SecretRedactor:
    """把已知密钥的**值**与常见凭据形态从任意结构化数据里抹掉。

    可直接交给 ``EventLog.set_redactor()``（签名就是 ``Callable[[Any], Any]``）::

        redactor = SecretRedactor()
        await redactor.bind_store(store)      # 登记当前已解锁库里的值
        event_log.set_redactor(redactor)

    行为：

    - 递归处理 dict / list / tuple / set / frozenset / str / bytes；
    - dict 的**键**也会被脱敏（键名里同样可能带密钥），代价是两个键脱敏后可能
      撞在一起——宁可丢一个键，不可漏一个值；
    - 已知值按子串替换；短于 ``min_literal_length`` 的值只在**整串相等**时替换，
      否则一个 2 字符的口令会把正常文本打得千疮百孔；
    - 正则兜底（sk- / xoxb- / ghp_ / Bearer / 私钥块 / .env 赋值 …）见
      :mod:`workerbee.security.redaction`；
    - **绝不抛异常**：内部出错时该字符串节点变成 ``[已阻止：脱敏失败]``。
      这是故意选择的「失败关闸」——脱敏器抛错会让调用方把原文直接写进日志。

    注意：本对象为了能替换，**必须持有明文值**。锁定 store 时请一并
    :meth:`clear`，否则明文仍驻留在脱敏器里（§15 二期换 keychain 后此项可
    改为按键取用）。
    """

    def __init__(
        self,
        secrets: Iterable[str] = (),
        *,
        mask: str = DEFAULT_MASK,
        min_literal_length: int = MIN_LITERAL_LENGTH,
    ) -> None:
        self.mask = mask
        self.min_literal_length = min_literal_length
        self._values: set[str] = set()
        self._scanner = OutboundScanner(
            mask=mask, known_values=(), min_literal_length=min_literal_length
        )
        self.bind_many(secrets)

    # ---- 已知值登记 ----

    def bind(self, value: str) -> None:
        """登记一个已知密钥值。空值与非字符串被忽略。"""
        if isinstance(value, str) and value and value not in self._values:
            self._values.add(value)
            self._rebuild()

    def bind_many(self, values: Iterable[str]) -> None:
        self._values.update(v for v in values if isinstance(v, str) and v)
        self._rebuild()

    def unbind(self, value: str) -> None:
        if value in self._values:
            self._values.discard(value)
            self._rebuild()

    def clear(self) -> None:
        """丢掉全部已登记的明文值。与 ``SecretStore.lock()`` 配套使用。"""
        self._values.clear()
        self._rebuild()

    @property
    def bound_count(self) -> int:
        return len(self._values)

    async def bind_store(self, store: "SecretStore") -> int:
        """登记一个**已解锁**库里的全部值，返回登记条数。

        锁定的库会抛 :class:`StoreLockedError`（而不是静默登记 0 条——静默会让
        调用方以为脱敏已生效）。
        """
        values = await store._all_values()
        self.bind_many(values)
        return len(values)

    def _rebuild(self) -> None:
        self._scanner = OutboundScanner(
            mask=self.mask,
            known_values=self._values,
            min_literal_length=self.min_literal_length,
        )

    def __repr__(self) -> str:  # 绝不显示已登记的值
        return (
            f"SecretRedactor(mask={self.mask!r}, "
            f"bound=<{len(self._values)} 个值已隐藏>)"
        )

    # ---- 脱敏 ----

    def redact_text(self, text: str) -> str:
        """只处理字符串。"""
        if not isinstance(text, str) or not text:
            return text
        try:
            return self._scanner.redact(text)
        except Exception:
            return REDACTION_FAILED

    def redact(self, value: Any) -> Any:
        """``__call__`` 的具名版本。"""
        return self(value)

    def __call__(self, value: Any) -> Any:
        """递归脱敏。这是 ``EventLog.set_redactor()`` 接受的入口。"""
        try:
            return self._walk(value)
        except Exception:
            # 走到这里说明结构本身出了意外（如自引用容器）。
            return REDACTION_FAILED

    def _walk(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, (bytes, bytearray)):
            # latin-1 逐字节映射，round-trip 无损；ASCII 形态的密钥照样能匹配。
            try:
                text = bytes(value).decode("latin-1")
                redacted = self.redact_text(text)
                out = redacted.encode("latin-1")
            except (UnicodeError, ValueError):
                return value
            return bytearray(out) if isinstance(value, bytearray) else out
        if isinstance(value, Mapping):
            return {self._walk(k): self._walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._walk(v) for v in value)
        if isinstance(value, set):
            return {self._walk(v) for v in value}
        if isinstance(value, frozenset):
            return frozenset(self._walk(v) for v in value)
        return value


# ---------------------------------------------------------------------------
# Secret Store
# ---------------------------------------------------------------------------


class SecretStore:
    """加密文件形式的凭据库（架构设计 v0.02 §5.5、§15）。

    生命周期::

        store = await SecretStore.create("口令", "~/.workerbee/secrets.vault")
        await store.put("secret://openai", {"api_key": "sk-…"})
        await store.lock()

        store = await SecretStore.open("口令", "~/.workerbee/secrets.vault")
        creds = await store.get("secret://openai")

    约定：

    - ``get`` 只返回**副本**，调用方改动不会影响库内状态；
    - 不存在的 locator 返回 ``None``；**已撤销**的 locator 抛
      :class:`SecretRevokedError`——撤销是治理动作，静默返回 None 会让调用方
      把「被撤销」误当成「没配过」；
    - ``revoke`` 会**丢弃**密值本身（撤销就该不可恢复），只保留元数据以便展示
      影响面（§9.1）；
    - ``put`` 拒绝覆盖已撤销的 locator，重新绑定必须显式走 ``rotate``。
    """

    def __init__(self, path: str | Path, *, params: KdfParams | None = None) -> None:
        self._path = Path(path)
        self._params = params or KdfParams()
        self._params.validate()
        self._salt: bytes | None = None
        self._key: bytearray | None = None
        self._secrets: dict[str, dict[str, str]] = {}
        self._meta: dict[str, SecretMeta] = {}
        self._locked = True
        self._io_lock = asyncio.Lock()

    # ---- 构造 ----

    @classmethod
    async def create(
        cls,
        passphrase: str,
        path: str | Path,
        *,
        params: KdfParams | None = None,
    ) -> "SecretStore":
        """新建一个空库。目标路径已存在时报错，绝不覆盖。"""
        _validate_passphrase(passphrase)
        store = cls(path, params=params)
        if store._path.exists():
            raise StoreExistsError(
                f"库文件已存在，create 不覆盖: {store._path}；"
                f"如确实要重建，请先自行备份并删除该文件"
            )
        store._salt = os.urandom(SALT_LEN)
        store._key = await asyncio.to_thread(
            _derive_key, passphrase, store._salt, store._params
        )
        store._secrets = {}
        store._meta = {}
        store._locked = False
        await store._flush()
        return store

    @classmethod
    async def open(cls, passphrase: str, path: str | Path) -> "SecretStore":
        """打开既有库。口令错误抛 :class:`PassphraseError`（绝不返回半开的库）。"""
        _validate_passphrase(passphrase)
        store = cls(path)
        blob = await asyncio.to_thread(store._read_bytes)
        vault = parse_vault(blob)
        store._params = vault.params
        store._salt = vault.salt
        key = await asyncio.to_thread(_derive_key, passphrase, vault.salt, vault.params)
        try:
            # 先过头部校验：口令/头部有问题时，连正文都不碰。
            await asyncio.to_thread(_verify_header, key, vault)
            plain_json = await asyncio.to_thread(_open_body, key, vault)
            secrets, meta = _decode_payload(plain_json)
        except Exception:
            _wipe(key)  # 任何失败路径都不留下派生密钥
            raise
        store._key = key
        store._secrets = secrets
        store._meta = meta
        store._locked = False
        return store

    # ---- 基本属性 ----

    @property
    def path(self) -> Path:
        return self._path

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def params(self) -> KdfParams:
        return self._params

    def __repr__(self) -> str:  # 绝不显示条目内容
        return (
            f"SecretStore(path={str(self._path)!r}, locked={self._locked}, "
            f"locators={len(self._meta)})"
        )

    # ---- 读写 ----

    async def put(
        self,
        locator: str,
        secret: Mapping[str, str],
        *,
        label: str | None = None,
    ) -> None:
        """新增或覆盖一条凭据。落盘前加密，写盘时重新生成 nonce。"""
        _validate_locator(locator)
        values = _validate_secret(secret, locator)
        async with self._io_lock:
            self._require_unlocked("put")
            existing = self._meta.get(locator)
            if existing is not None and existing.revoked:
                raise SecretRevokedError(
                    f"locator 已撤销，不能直接覆盖: {locator}；"
                    f"如确需重新绑定请显式调用 rotate()"
                )
            now = utcnow().isoformat()
            self._secrets[locator] = values
            self._meta[locator] = SecretMeta(
                locator=locator,
                label=label or (existing.label if existing else _default_label(locator)),
                created_at=existing.created_at if existing else now,
                updated_at=now,
                revoked=False,
            )
            await self._flush()

    async def get(self, locator: str) -> dict[str, str] | None:
        """取一条凭据的值（副本）。不存在返回 None；已撤销抛异常；未解锁抛异常。"""
        _validate_locator(locator)
        async with self._io_lock:
            self._require_unlocked("get")
            meta = self._meta.get(locator)
            if meta is None:
                return None
            if meta.revoked:
                raise SecretRevokedError(f"locator 已撤销，材料不可读: {locator}")
            return dict(self._secrets.get(locator, {}))

    async def revoke(self, locator: str) -> bool:
        """撤销一条凭据：**丢弃密值**，保留元数据。返回是否命中已有条目。"""
        _validate_locator(locator)
        async with self._io_lock:
            self._require_unlocked("revoke")
            meta = self._meta.get(locator)
            if meta is None:
                return False
            if meta.revoked:
                return True  # 幂等：重复撤销不重复改写
            material = self._secrets.pop(locator, None)
            if material is not None:
                material.clear()  # 副本先清，再丢引用
            self._meta[locator] = replace(
                meta, revoked=True, updated_at=utcnow().isoformat()
            )
            await self._flush()
            return True

    async def rotate(
        self,
        locator: str,
        new_secret: Mapping[str, str],
        *,
        label: str | None = None,
    ) -> None:
        """换新密值。已撤销的 locator 会因本调用**重新生效**（这是显式的重新绑定）。"""
        _validate_locator(locator)
        values = _validate_secret(new_secret, locator)
        async with self._io_lock:
            self._require_unlocked("rotate")
            meta = self._meta.get(locator)
            if meta is None:
                raise SecretNotFoundError(
                    f"locator 不存在，无法 rotate: {locator}（新增请用 put()）"
                )
            self._secrets[locator] = values
            self._meta[locator] = replace(
                meta,
                label=label or meta.label,
                updated_at=utcnow().isoformat(),
                revoked=False,
            )
            await self._flush()

    async def list_locators(self) -> list[SecretMeta]:
        """列出元数据（locator / label / 创建与更新时间 / 是否撤销）。

        **只返回元数据，绝不返回值**；未解锁时同样可用——治理与影响面展示
        （§9.1）不该因为用户把库锁上了就看不见。
        """
        async with self._io_lock:
            return [self._meta[k] for k in sorted(self._meta)]

    # ---- 生命周期 ----

    async def lock(self) -> None:
        """清空内存中的明文与派生密钥。

        做的是「本对象不再持有明文引用」：值 dict 就地 clear 后丢弃，派生密钥
        （``bytearray``）就地清零。CPython 不保证物理擦除，详见模块 docstring。
        """
        async with self._io_lock:
            for material in self._secrets.values():
                material.clear()
            self._secrets.clear()
            if self._key is not None:
                _wipe(self._key)
                self._key = None
            self._locked = True

    async def change_passphrase(self, old: str, new: str) -> None:
        """改口令。旧口令不对抛 :class:`PassphraseError`，数据不动。"""
        _validate_passphrase(old)
        _validate_passphrase(new)
        async with self._io_lock:
            self._require_unlocked("change_passphrase")
            salt = self._salt
            current = self._key
            if salt is None or current is None:  # 与 _require_unlocked 同义的兜底
                raise StoreLockedError("库未解锁，无法改口令")
            derived = await asyncio.to_thread(_derive_key, old, salt, self._params)
            # 定时安全比较，避免「先比到哪一位」泄漏信息；bytearray 可直接比较，
            # 不额外产生一份不可擦除的 bytes 副本。
            ok = hmac.compare_digest(derived, current)
            _wipe(derived)
            if not ok:
                raise PassphraseError(
                    f"旧口令不正确，未做任何改动: {self._path}"
                )

            # 先按新盐新钥生成新文件并落盘，成功后才切换内存状态：
            # 任何一步失败，内存与磁盘都还是旧口令那一版。
            new_salt = os.urandom(SALT_LEN)
            new_key = await asyncio.to_thread(_derive_key, new, new_salt, self._params)
            try:
                blob = await asyncio.to_thread(self._seal_with, new_salt, new_key)
                await asyncio.to_thread(_write_atomic, self._path, blob)
            except Exception:
                _wipe(new_key)
                raise
            old_key = self._key
            self._salt = new_salt
            self._key = new_key
            if old_key is not None:
                _wipe(old_key)

    # ---- 内部：状态与持久化 ----

    def _require_unlocked(self, action: str) -> None:
        if self._locked or self._key is None:
            raise StoreLockedError(
                f"库未解锁，无法执行 {action}；请先 await SecretStore.open(...)"
            )

    def _read_bytes(self) -> bytes:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            raise StoreNotFoundError(
                f"库文件不存在: {self._path}；新库请用 SecretStore.create()"
            ) from None
        except OSError as exc:
            raise SecretStoreError(
                f"库文件不可读: {self._path}（{type(exc).__name__}）"
            ) from None

    def _payload(self) -> dict[str, Any]:
        """内存态 → 待加密的明文结构。这是唯一会把值写进缓冲区的地方。"""
        return {
            "format": "workerbee-secret-store",
            "version": VAULT_VERSION,
            "entries": {
                locator: {
                    "secret": dict(self._secrets.get(locator, {})),
                    "label": meta.label,
                    "created_at": meta.created_at,
                    "updated_at": meta.updated_at,
                    "revoked": meta.revoked,
                }
                for locator, meta in self._meta.items()
            },
        }

    def _seal_with(self, salt: bytes, key: bytearray) -> bytes:
        """用给定盐与密钥封存当前内存态，返回完整文件字节。"""
        aad = _pack_aad(VAULT_VERSION, self._params, salt)
        aes = AESGCM(key)
        plaintext = json.dumps(
            self._payload(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        buf = bytearray(plaintext)
        try:
            body_nonce = os.urandom(NONCE_LEN)  # 每次写盘重新生成，绝不复用
            body_ct = aes.encrypt(body_nonce, buf, aad)
        finally:
            _wipe(buf)
        kcv_nonce = os.urandom(NONCE_LEN)
        kcv_ct = aes.encrypt(kcv_nonce, KCV_PLAINTEXT, aad)
        return (
            _PREFIX.pack(
                MAGIC,
                VAULT_VERSION,
                self._params.time_cost,
                self._params.memory_cost,
                self._params.parallelism,
                self._params.hash_len,
            )
            + salt
            + kcv_nonce
            + kcv_ct
            + body_nonce
            + body_ct
        )

    async def _flush(self) -> None:
        salt, key = self._salt, self._key
        if salt is None or key is None:
            raise StoreLockedError("库未解锁，无法写盘")
        blob = await asyncio.to_thread(self._seal_with, salt, key)
        await asyncio.to_thread(_write_atomic, self._path, blob)

    async def _all_values(self) -> list[str]:
        """全部明文值。**仅供同模块的 SecretRedactor 使用**，不对外暴露。"""
        async with self._io_lock:
            self._require_unlocked("_all_values")
            return [v for material in self._secrets.values() for v in material.values()]


# ---------------------------------------------------------------------------
# 内部：解密与载荷解析（模块级函数，便于测试直接打靶）
# ---------------------------------------------------------------------------


def _verify_header(key: bytearray, vault: VaultFile) -> None:
    """校验头部（含 AAD 绑定的一切）。失败即抛，绝不带出任何明文。"""
    try:
        plain = AESGCM(key).decrypt(vault.kcv_nonce, vault.kcv_ct, vault.aad)
    except InvalidTag:
        raise PassphraseError(
            "无法解锁：头部校验失败——口令错误，或文件头（版本 / Argon2 参数 / 盐）"
            "被篡改或损坏。AES-GCM 无法区分这两种情况，故不做猜测。"
        ) from None
    except Exception as exc:
        raise VaultIntegrityError(
            f"头部校验异常（{type(exc).__name__}），拒绝继续"
        ) from None
    if plain != KCV_PLAINTEXT:
        raise VaultIntegrityError("头部校验值内容异常，拒绝继续")


def _open_body(key: bytearray, vault: VaultFile) -> str:
    """解密正文，返回明文 JSON 文本。

    密钥与头部均已通过校验，此时失败只可能是正文被改动——因此报的是「损坏／被
    篡改」，而不是「口令错误」。
    """
    try:
        plain = AESGCM(key).decrypt(vault.body_nonce, vault.body_ct, vault.aad)
    except InvalidTag:
        raise StoreCorruptError(
            "密文本体完整性校验失败：文件被篡改或损坏，拒绝返回任何数据"
        ) from None
    except Exception as exc:
        raise StoreCorruptError(f"解密失败（{type(exc).__name__}），拒绝返回任何数据") from None
    buf = bytearray(plain)
    try:
        return buf.decode("utf-8")
    finally:
        _wipe(buf)


def _decode_payload(text: str) -> tuple[dict[str, dict[str, str]], dict[str, SecretMeta]]:
    """把解密出的 JSON 还原成内存态。结构不对即报错，不猜测。"""
    try:
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        raise StoreCorruptError("密文解密成功但内容不是合法 JSON：文件已损坏") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        raise StoreCorruptError("载荷结构不合法（缺少 entries）")
    secrets: dict[str, dict[str, str]] = {}
    meta: dict[str, SecretMeta] = {}
    for locator, entry in payload["entries"].items():
        if not isinstance(entry, dict):
            raise StoreCorruptError(f"载荷条目结构不合法: {locator}")
        material = entry.get("secret") or {}
        if not isinstance(material, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in material.items()
        ):
            raise StoreCorruptError(f"载荷条目内容不合法: {locator}")
        secrets[locator] = dict(material)
        meta[locator] = SecretMeta(
            locator=locator,
            label=str(entry.get("label") or _default_label(locator)),
            created_at=str(entry.get("created_at") or ""),
            updated_at=str(entry.get("updated_at") or ""),
            revoked=bool(entry.get("revoked", False)),
        )
    return secrets, meta


def _validate_locator(locator: Any) -> str:
    if not isinstance(locator, str) or not locator.strip():
        raise ValueError("locator 必须是非空字符串")
    return locator


def _validate_secret(secret: Any, locator: str) -> dict[str, str]:
    """校验并复制。错误消息只带 locator——**绝不回显值**（AUTH-02）。"""
    if not isinstance(secret, Mapping):
        raise TypeError(f"secret 必须是 dict[str, str]（locator={locator}）")
    out: dict[str, str] = {}
    for key, value in secret.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(f"secret 的键与值都必须是 str（locator={locator}）")
        out[key] = value
    if not out:
        raise ValueError(f"secret 不能为空（locator={locator}）")
    return out
