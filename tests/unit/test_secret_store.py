"""Secret Store：加解密、口令、nonce、锁定、篡改检测与脱敏器。

对应 AUTH-02（凭据本体不出 L5）、§9.1（凭据治理）、§15（Secret Store 首版实现）。

一条贯穿全部用例的纪律：**断言里不出现明文值**。需要验证「值有没有泄漏」时，
一律用「明文是否出现在密文文件里」这类形态判断，而不是把值打印出来对拍。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from workerbee.data.db import Database
from workerbee.data.event_log import EventLog, EventScope, EventType
from workerbee.security import secret_store as ss
from workerbee.security.secret_store import (
    KdfParams,
    PassphraseError,
    SecretNotFoundError,
    SecretRedactor,
    SecretRevokedError,
    SecretStore,
    StoreCorruptError,
    StoreExistsError,
    StoreFormatError,
    StoreLockedError,
    StoreNotFoundError,
    VaultIntegrityError,
    read_vault,
)

pytestmark = pytest.mark.unit

PASSPHRASE = "correct horse battery staple"

#: 轻量 KDF 参数：大多数用例检验的是**格式与生命周期**，不是 Argon2 的抗爆破强度。
#: 涉及「口令派生正确性」的用例（错误口令、改口令）一律用默认参数，保持代表性。
LIGHT = KdfParams(time_cost=1, memory_cost=8192, parallelism=1)

# 头部字段偏移，对应 secret_store 模块 docstring 里的文件格式：
# magic(8) version(2) time_cost(4) memory_cost(4) parallelism(4) hash_len(1) salt(16) …
OFF_VERSION = 8
OFF_TIME_COST = 10
OFF_MEMORY_COST = 14


async def new_store(tmp_path: Path, *, params: KdfParams = LIGHT, passphrase: str = PASSPHRASE):
    return await SecretStore.create(passphrase, tmp_path / "vault.bin", params=params)


def flip_bit(path: Path, offset: int) -> None:
    """翻转文件中某一字节的最低位。"""
    blob = bytearray(path.read_bytes())
    blob[offset] ^= 0x01
    path.write_bytes(bytes(blob))


def read_field(path: Path, offset: int, length: int) -> bytes:
    return path.read_bytes()[offset : offset + length]


def patch_field(path: Path, offset: int, raw: bytes) -> None:
    blob = bytearray(path.read_bytes())
    blob[offset : offset + len(raw)] = raw
    path.write_bytes(bytes(blob))


def vault_bytes(path: Path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# 加解密 round-trip 与持久化
# ---------------------------------------------------------------------------


async def test_put_get_roundtrip(tmp_path):
    store = await new_store(tmp_path)
    await store.put("secret://openai", {"api_key": "sk-test-VALUE-0001", "org": "acme"})
    assert await store.get("secret://openai") == {
        "api_key": "sk-test-VALUE-0001",
        "org": "acme",
    }


async def test_get_returns_a_copy(tmp_path):
    """调用方改动返回值不得影响库内状态。"""
    store = await new_store(tmp_path)
    await store.put("secret://a", {"k": "v-original"})
    got = await store.get("secret://a")
    got["k"] = "mutated-by-caller"
    assert await store.get("secret://a") == {"k": "v-original"}


async def test_reopen_reads_back(tmp_path):
    """写盘后重新 open 能读回（默认参数，覆盖真实派生路径）。"""
    store = await SecretStore.create(PASSPHRASE, tmp_path / "vault.bin")
    await store.put("secret://a", {"token": "tok-REOPEN-0001"})
    await store.put("secret://b", {"token": "tok-REOPEN-0002"})
    await store.lock()

    reopened = await SecretStore.open(PASSPHRASE, tmp_path / "vault.bin")
    assert await reopened.get("secret://a") == {"token": "tok-REOPEN-0001"}
    assert await reopened.get("secret://b") == {"token": "tok-REOPEN-0002"}


async def test_unknown_locator_returns_none(tmp_path):
    store = await new_store(tmp_path)
    assert await store.get("secret://never-created") is None


async def test_revoke_rotate_and_put_on_unknown_paths(tmp_path):
    store = await new_store(tmp_path)
    assert await store.revoke("secret://never-created") is False
    with pytest.raises(SecretNotFoundError):
        await store.rotate("secret://never-created", {"k": "v"})


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------


async def test_wrong_passphrase_raises_instead_of_returning_none(tmp_path):
    """口令错误必须抛明确异常——不返回 None，也不返回乱码。"""
    store = await SecretStore.create(PASSPHRASE, tmp_path / "vault.bin")
    await store.put("secret://a", {"api_key": "sk-test-WRONGPASS-01"})

    with pytest.raises(PassphraseError) as excinfo:
        await SecretStore.open("not-the-passphrase", tmp_path / "vault.bin")
    assert isinstance(excinfo.value, VaultIntegrityError)
    # 异常消息里不能出现口令，也不能出现任何值。
    assert "not-the-passphrase" not in str(excinfo.value)
    assert "sk-test-WRONGPASS-01" not in str(excinfo.value)


async def test_empty_passphrase_rejected(tmp_path):
    with pytest.raises(ValueError):
        await SecretStore.create("", tmp_path / "vault.bin")


async def test_change_passphrase_switches_key(tmp_path):
    path = tmp_path / "vault.bin"
    store = await SecretStore.create(PASSPHRASE, path)
    await store.put("secret://a", {"api_key": "sk-test-CHANGE-0001"})
    salt_before = read_vault(path).salt

    await store.change_passphrase(PASSPHRASE, "a-brand-new-passphrase")

    # 旧口令失效，新口令可读，数据原样保留。
    with pytest.raises(PassphraseError):
        await SecretStore.open(PASSPHRASE, path)
    reopened = await SecretStore.open("a-brand-new-passphrase", path)
    assert await reopened.get("secret://a") == {"api_key": "sk-test-CHANGE-0001"}
    # 换口令同时换盐，避免新旧口令派生出同一把密钥。
    assert read_vault(path).salt != salt_before


async def test_change_passphrase_with_wrong_old_keeps_vault_intact(tmp_path):
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-KEEP-0001"})

    with pytest.raises(PassphraseError):
        await store.change_passphrase("wrong-old-passphrase", "new-passphrase")

    reopened = await SecretStore.open(PASSPHRASE, path)
    assert await reopened.get("secret://a") == {"api_key": "sk-test-KEEP-0001"}


# ---------------------------------------------------------------------------
# nonce 与密文
# ---------------------------------------------------------------------------


async def test_same_plaintext_encrypts_differently_every_write(tmp_path):
    """同一明文两次 put，密文与 nonce 都必须不同（nonce 绝不复用）。"""
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-NONCE-0001"})
    first = read_vault(path)

    await store.put("secret://a", {"api_key": "sk-test-NONCE-0001"})
    second = read_vault(path)

    # 正文与头部校验值各自持有独立 nonce，两次写入都必须是新生成的。
    assert first.body_nonce != second.body_nonce
    assert first.kcv_nonce != second.kcv_nonce
    # 密文随之不同。注意 updated_at 也在变，所以真正承重的断言是上面的 nonce。
    assert first.body_ct != second.body_ct


async def test_plaintext_never_appears_in_vault_file(tmp_path):
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-PLAINTEXT-LEAK-0001"})
    assert b"sk-test-PLAINTEXT-LEAK-0001" not in vault_bytes(path)
    await store.lock()
    assert b"sk-test-PLAINTEXT-LEAK-0001" not in vault_bytes(path)


# ---------------------------------------------------------------------------
# 锁定
# ---------------------------------------------------------------------------


async def test_lock_clears_plaintext_and_blocks_reads(tmp_path):
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-LOCK-0001"})

    await store.lock()
    assert store.locked is True
    with pytest.raises(StoreLockedError):
        await store.get("secret://a")
    with pytest.raises(StoreLockedError):
        await store.put("secret://b", {"k": "v"})
    with pytest.raises(StoreLockedError):
        await store.change_passphrase(PASSPHRASE, "whatever")


async def test_lock_really_empties_memory(tmp_path):
    """`lock()` 必须是真清空：明文 dict 清掉、派生密钥归零后丢弃，而非只置标志位。"""
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-MEMORY-0001"})
    assert store._key is not None and any(store._key)  # 解锁时确实持有密钥

    await store.lock()

    assert store._secrets == {}
    assert store._key is None


async def test_list_locators_still_works_when_locked(tmp_path):
    """元数据不是秘密：锁上以后仍要能看影响面（§9.1）。"""
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-META-0001"})
    await store.lock()

    metas = await store.list_locators()
    assert [m.locator for m in metas] == ["secret://a"]


async def test_list_locators_never_returns_values(tmp_path):
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-META-LEAK-0001"})
    metas = await store.list_locators()

    assert len(metas) == 1
    meta = metas[0]
    assert (meta.locator, meta.label, meta.revoked) == ("secret://a", "a", False)
    assert meta.created_at and meta.updated_at

    text = repr(meta) + repr(store) + repr(metas)
    assert "sk-test-META-LEAK-0001" not in text


# ---------------------------------------------------------------------------
# 撤销与轮换
# ---------------------------------------------------------------------------


async def test_revoke_drops_material_and_keeps_metadata(tmp_path):
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-REVOKE-0001"})

    assert await store.revoke("secret://a") is True

    with pytest.raises(SecretRevokedError):
        await store.get("secret://a")
    metas = await store.list_locators()
    assert metas[0].revoked is True
    # 撤销是治理动作，值本身不再留在文件里。
    assert b"sk-test-REVOKE-0001" not in vault_bytes(path)
    # 幂等：重复撤销不重复改写，也不报错。
    assert await store.revoke("secret://a") is True


async def test_revoked_locator_cannot_be_overwritten_but_can_be_rotated(tmp_path):
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-REVOKED-0001"})
    await store.revoke("secret://a")

    with pytest.raises(SecretRevokedError):
        await store.put("secret://a", {"api_key": "sk-test-REBIND-0001"})

    # rotate 是显式的重新绑定动作，允许复活。
    await store.rotate("secret://a", {"api_key": "sk-test-ROTATED-0001"})
    assert await store.get("secret://a") == {"api_key": "sk-test-ROTATED-0001"}
    assert (await store.list_locators())[0].revoked is False


# ---------------------------------------------------------------------------
# 文件格式与篡改检测
# ---------------------------------------------------------------------------


async def test_create_refuses_to_overwrite_existing_vault(tmp_path):
    path = tmp_path / "vault.bin"
    await new_store(tmp_path)
    with pytest.raises(StoreExistsError):
        await SecretStore.create(PASSPHRASE, path, params=LIGHT)


async def test_open_missing_file_raises(tmp_path):
    with pytest.raises(StoreNotFoundError):
        await SecretStore.open(PASSPHRASE, tmp_path / "nope.bin")


async def test_garbage_file_rejected(tmp_path):
    path = tmp_path / "vault.bin"
    path.write_bytes(b"definitely not a workerbee vault" * 8)
    with pytest.raises(StoreFormatError):
        await SecretStore.open(PASSPHRASE, path)


async def test_tampered_ciphertext_is_detected(tmp_path):
    """翻转密文正文里的一字节：必须失败，不能静默返回错误数据。"""
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-TAMPER-BODY-0001"})

    flip_bit(path, ss._BODY_CT_OFFSET + 5)

    with pytest.raises(StoreCorruptError) as excinfo:
        await SecretStore.open(PASSPHRASE, path)
    assert isinstance(excinfo.value, VaultIntegrityError)
    assert "sk-test-TAMPER-BODY-0001" not in str(excinfo.value)


async def test_truncated_ciphertext_is_detected(tmp_path):
    """砍掉正文尾部（GCM tag 的一部分）：长度还够解析，于是由完整性校验拦住。"""
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-TRUNCATE-0001"})
    blob = path.read_bytes()
    assert len(blob) - 8 >= ss._MIN_FILE_LEN

    path.write_bytes(blob[:-8])

    with pytest.raises(StoreCorruptError):
        await SecretStore.open(PASSPHRASE, path)


async def test_short_file_is_rejected_before_any_crypto(tmp_path):
    """短到装不下文件头的输入，在结构校验阶段就被拒，连派生都不会跑。"""
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-TRUNCATE-0002"})

    path.write_bytes(path.read_bytes()[: ss._BODY_CT_OFFSET + 4])

    with pytest.raises(StoreFormatError):
        await SecretStore.open(PASSPHRASE, path)


async def test_tampered_argon2_params_are_detected(tmp_path):
    """改掉文件里的 Argon2 参数：必须被拒，且不得被当成「参数还能用」接受。

    这一版里参数既进 KDF 又进 AAD（见模块 docstring 的诚实说明），因此两条防线
    同时生效；关键行为是**报错**，不是静默用错误参数解开。
    """
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-TAMPER-PARAMS-0001"})
    original = read_vault(path).params

    # time_cost 1 → 2：仍在合法区间内，绕不过范围校验，只能靠完整性校验拦住。
    patch_field(path, OFF_TIME_COST, (2).to_bytes(4, "big"))
    assert read_vault(path).params.time_cost == 2  # 文件确实被改了
    assert read_vault(path).params != original

    with pytest.raises(VaultIntegrityError):
        await SecretStore.open(PASSPHRASE, path)


async def test_out_of_range_params_rejected_before_key_derivation(tmp_path, monkeypatch):
    """越界参数必须在派生**之前**被拒——否则一个被改大的 memory_cost 就是拒绝服务。"""
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-DOS-0001"})

    # memory_cost 8 MiB → 1 TiB：若先派生，这里会去尝试申请巨量内存。
    patch_field(path, OFF_MEMORY_COST, (1 << 30).to_bytes(4, "big"))
    called: list[int] = []
    monkeypatch.setattr(ss, "_derive_key", lambda *a, **k: called.append(1))

    with pytest.raises(StoreFormatError):
        await SecretStore.open(PASSPHRASE, path)
    assert called == [], "参数校验必须先于密钥派生"


async def test_tampered_version_byte_is_detected(tmp_path):
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-TAMPER-VERSION-0001"})

    flip_bit(path, OFF_VERSION)

    with pytest.raises(StoreFormatError):
        await SecretStore.open(PASSPHRASE, path)


async def test_tampered_salt_is_detected(tmp_path):
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-TAMPER-SALT-0001"})

    flip_bit(path, ss._SALT_OFFSET)

    with pytest.raises(VaultIntegrityError):
        await SecretStore.open(PASSPHRASE, path)


def test_aad_binds_version_params_and_salt():
    """AAD 必须覆盖 version / Argon2 参数 / 盐——这是「参数被改后仍能解密」的防线。"""
    params = KdfParams()
    salt = bytes(range(16))
    base = ss._pack_aad(1, params, salt)

    assert ss._pack_aad(2, params, salt) != base
    assert ss._pack_aad(1, KdfParams(time_cost=4), salt) != base
    assert ss._pack_aad(1, KdfParams(memory_cost=131072), salt) != base
    assert ss._pack_aad(1, KdfParams(parallelism=2), salt) != base
    assert ss._pack_aad(1, params, bytes(16)) != base
    assert len(base) == ss._AAD_LEN


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位")
async def test_vault_file_is_owner_only(tmp_path):
    path = tmp_path / "vault.bin"
    await new_store(tmp_path)
    assert path.stat().st_mode & 0o077 == 0


async def test_parse_vault_never_returns_plaintext(tmp_path):
    """`read_vault` 是排查入口，必须只给结构、不给内容。"""
    path = tmp_path / "vault.bin"
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": "sk-test-INSPECT-0001"})

    described = read_vault(path).describe()
    assert "sk-test-INSPECT-0001" not in described
    assert "argon2id" in described


# ---------------------------------------------------------------------------
# 输入校验：错误消息里绝不回显值
# ---------------------------------------------------------------------------


async def test_invalid_secret_errors_do_not_echo_values(tmp_path):
    store = await new_store(tmp_path)
    with pytest.raises(TypeError) as excinfo:
        await store.put("secret://a", {"api_key": 1234567890})
    assert "1234567890" not in str(excinfo.value)

    with pytest.raises(ValueError):
        await store.put("secret://a", {})

    with pytest.raises(ValueError):
        await store.put("", {"k": "v"})


# ---------------------------------------------------------------------------
# SecretRedactor
# ---------------------------------------------------------------------------

KNOWN_SECRET = "sk-known-VALUE-abcdefghijklmnop"
OTHER_SECRET = "hunter2-the-password"


async def test_redactor_masks_known_values_in_nested_structures():
    redactor = SecretRedactor([KNOWN_SECRET, OTHER_SECRET])
    payload = {
        "note": f"key is {KNOWN_SECRET} ok",
        "nested": {
            "list": [f"prefix {OTHER_SECRET} suffix", 42, None],
            "tuple": (f"{KNOWN_SECRET}",),
        },
        "untouched": "the quick brown fox jumps over the lazy dog",
        "number": 7,
    }

    out = redactor(payload)

    assert out["note"] == "key is *** ok"
    assert out["nested"]["list"][0] == "prefix *** suffix"
    assert out["nested"]["list"][1:] == [42, None]
    assert out["nested"]["tuple"] == ("***",)
    assert out["untouched"] == "the quick brown fox jumps over the lazy dog"
    assert out["number"] == 7
    assert KNOWN_SECRET not in repr(out) and OTHER_SECRET not in repr(out)


def test_redactor_masks_credential_shapes_without_registration():
    """正则兜底：没登记过的常见凭据形态也要被抹掉。"""
    redactor = SecretRedactor()
    text = (
        "openai=sk-proj-abcdefghijklmnopqrstuvwxyz012345 "
        "slack=xoxb-123456789012-abcdefghijklmn "
        "github=ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
        "auth: Bearer abcdefghijklmnopqrstuvwxyz0123"
    )
    out = redactor(text)

    for marker in ("sk-proj-", "xoxb-", "ghp_", "abcdefghijklmnopqrstuvwxyz0123"):
        assert marker not in out
    assert "***" in out


def test_redactor_leaves_clean_content_alone():
    """非敏感内容不得被误改。"""
    redactor = SecretRedactor([KNOWN_SECRET])
    clean = (
        "def add(a, b):\n    return a + b\n\n"
        "The pipeline reads artifacts from the store and forwards them downstream.\n"
        "See https://example.com/docs/architecture for the full picture."
    )
    assert redactor(clean) == clean
    assert redactor({"file": "src/workerbee/core/graph/derive.py", "line": 42}) == {
        "file": "src/workerbee/core/graph/derive.py",
        "line": 42,
    }


def test_redactor_short_values_only_match_whole_strings():
    """短值不做子串替换：否则一个 2 字符口令会把正常文本打碎。"""
    redactor = SecretRedactor(["abc"], min_literal_length=4)
    assert redactor("abc") == "***"
    assert redactor("xabcx") == "xabcx"


def test_redactor_repr_hides_values():
    redactor = SecretRedactor([KNOWN_SECRET])
    assert KNOWN_SECRET not in repr(redactor)


def test_redactor_dedupes_registrations_and_clears():
    """clear() 之后不再按已知值替换。

    这里故意用一个**不像凭据**的值：像 ``sk-…`` 那种形态会被正则兜底继续抹掉，
    那正是兜底该有的行为，拿来测 clear() 会得到误导性的结论。
    """
    plain = "plain-value-0001"
    redactor = SecretRedactor([plain, plain, ""])
    assert redactor.bound_count == 1
    assert redactor(f"x {plain} y") == "x *** y"

    redactor.clear()
    assert redactor.bound_count == 0
    assert redactor(f"x {plain} y") == f"x {plain} y"


async def test_redactor_binds_values_from_an_unlocked_store(tmp_path):
    store = await new_store(tmp_path)
    await store.put("secret://a", {"api_key": KNOWN_SECRET})

    redactor = SecretRedactor()
    assert await redactor.bind_store(store) == 1

    # 密钥出现在自由文本里时也要被抹掉——这正是「已知值替换」的价值。
    out = redactor(f"the key is {KNOWN_SECRET}, do not share")
    assert out == "the key is ***, do not share"

    await store.lock()
    with pytest.raises(StoreLockedError):
        await SecretRedactor().bind_store(store)


async def test_redactor_is_accepted_by_event_log(tmp_path):
    """EventLog.set_redactor 的契约是 Callable[[Any], Any]，本对象直接可用。"""
    db = Database(tmp_path / "wb.db")
    await db.connect()
    try:
        log = EventLog(db)
        log.set_redactor(SecretRedactor([KNOWN_SECRET]))
        await log.append(
            scope=EventScope.TASK,
            type=EventType.TASK_SUBMITTED,
            payload={"note": f"bound {KNOWN_SECRET}", "raw": "sk-proj-abcdefghijklmnopqrstuv"},
        )
        rows = await log.tail()
        assert KNOWN_SECRET not in str(rows)
        assert "sk-proj-" not in str(rows)
        assert rows[0]["payload"]["note"] == "bound ***"
    finally:
        await db.close()
