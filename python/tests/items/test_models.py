"""Tests for items/models.py — domain types ported from Go.

Covers:
- Enum values match Go's string constants.
- ContextItem / Project / NewItemParams dataclass shapes.
- new_context_item happy path + every validation rule from Go's
  validateCombination (one positive + one negative case per rule).
- count_words: ASCII, empty, whitespace-only, CJK per-ideograph behavior.
- new_project validation + defaults.
"""

from __future__ import annotations

import uuid

import pytest

from unictx.items.errors import ItemValidationError
from unictx.items.models import (
    CONTENT_INLINE_LIMIT,
    ContextItem,
    Kind,
    NewItemParams,
    Project,
    Scope,
    Source,
    Visibility,
    count_words,
    new_context_item,
    new_project,
)

# ---------------------------------------------------------------------------
# Enum values — must match Go's string constants exactly.
# ---------------------------------------------------------------------------


def test_scope_values():
    assert Scope.USER == "user"
    assert Scope.PROJECT == "project"
    assert Scope.GLOBAL == "global"


def test_kind_values():
    assert Kind.NOTE == "note"
    assert Kind.EXCERPT == "excerpt"
    assert Kind.LINK == "link"
    assert Kind.DOC == "doc"
    assert Kind.CONVERSATION_MSG == "conversation_msg"
    assert Kind.MEMORY == "memory"
    assert Kind.FILE == "file"


def test_source_values():
    assert Source.MANUAL == "manual"
    assert Source.AGENT == "agent"
    assert Source.SYNC == "sync"
    assert Source.IMPORT == "import"
    assert Source.WEBHOOK == "webhook"


def test_visibility_values():
    assert Visibility.PRIVATE == "private"
    assert Visibility.PROJECT == "project"
    assert Visibility.PUBLIC == "public"


def test_content_inline_limit():
    assert CONTENT_INLINE_LIMIT == 4 * 1024


# ---------------------------------------------------------------------------
# ContextItem / Project dataclass shape.
# ---------------------------------------------------------------------------


def test_context_item_defaults():
    """Default-constructed ContextItem exposes Go-equivalent zero values."""
    item = ContextItem()
    assert item.id == ""
    assert item.scope == Scope.USER
    assert item.kind == Kind.NOTE
    assert item.source == Source.MANUAL
    assert item.visibility == Visibility.PRIVATE
    assert item.confidence == 1.0
    assert item.tags == []
    assert item.source_meta == {}
    assert item.any_embedding == 0
    assert item.version == 1
    assert item.word_count == 0
    # Mutating default-factory collections should not leak between instances.
    item.tags.append("x")
    item.source_meta["k"] = "v"
    other = ContextItem()
    assert other.tags == []
    assert other.source_meta == {}


def test_context_item_slots():
    """slots=True forbids adding undeclared attributes."""
    item = ContextItem()
    with pytest.raises(AttributeError):
        item.bogus_field = "nope"  # type: ignore[attr-defined]


def test_project_defaults():
    p = Project()
    assert p.id == ""
    assert p.name == ""
    assert p.path == ""
    assert p.description == ""
    assert p.created_at == 0
    assert p.updated_at == 0


def test_project_slots():
    p = Project()
    with pytest.raises(AttributeError):
        p.bogus = 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# new_context_item happy path.
# ---------------------------------------------------------------------------


def test_new_context_item_user_scope_happy():
    params = NewItemParams(owner_user_id="u1")
    item = new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, params)
    # Identity fields.
    assert item.scope == Scope.USER
    assert item.kind == Kind.NOTE
    assert item.source == Source.MANUAL
    assert item.owner_user_id == "u1"
    assert item.project_id == ""
    assert item.agent_id == ""
    # Defaults Go sets inside NewContextItem.
    assert item.visibility == Visibility.PRIVATE
    assert item.confidence == 1.0
    assert item.tags == []
    assert item.source_meta == {}
    assert item.version == 1
    assert item.any_embedding == 0
    # ID is a uuid7 (sortable, time-ordered — first 48 bits are ms timestamp).
    parsed = uuid.UUID(item.id)
    assert parsed.version == 7
    # Timestamps populated as integers.
    assert isinstance(item.created_at, int)
    assert isinstance(item.updated_at, int)
    assert item.created_at > 0
    assert item.created_at == item.updated_at


def test_new_context_item_global_happy():
    item = new_context_item(Scope.GLOBAL, Kind.NOTE, Source.MANUAL, NewItemParams())
    assert item.scope == Scope.GLOBAL
    assert item.owner_user_id == ""


def test_new_context_item_project_scope_happy():
    params = NewItemParams(project_id="p1", agent_id="a1")
    item = new_context_item(Scope.PROJECT, Kind.MEMORY, Source.AGENT, params)
    assert item.project_id == "p1"
    assert item.agent_id == "a1"


def test_new_context_item_id_is_unique_and_sortable():
    """uuid7 is monotonic — earlier id sorts before later id."""
    a = new_context_item(Scope.GLOBAL, Kind.NOTE, Source.MANUAL, NewItemParams())
    b = new_context_item(Scope.GLOBAL, Kind.NOTE, Source.MANUAL, NewItemParams())
    assert a.id != b.id
    assert a.id < b.id  # uuid7 string sort == time order


def test_new_context_item_word_count_from_content():
    item = new_context_item(
        Scope.GLOBAL,
        Kind.NOTE,
        Source.MANUAL,
        NewItemParams(),
        content="hello world foo",
    )
    assert item.word_count == 3


def test_new_context_item_copies_tags_and_source_meta():
    """Factory must not retain caller's mutable collection identity."""
    tags = ["a", "b"]
    meta = {"k": "v"}
    item = new_context_item(
        Scope.GLOBAL,
        Kind.NOTE,
        Source.MANUAL,
        NewItemParams(),
        tags=tags,
        source_meta=meta,
    )
    assert item.tags == ["a", "b"]
    assert item.source_meta == {"k": "v"}
    tags.append("c")
    meta["x"] = "y"
    assert item.tags == ["a", "b"]
    assert item.source_meta == {"k": "v"}


# ---------------------------------------------------------------------------
# validateCombination — every rule from Go, positive + negative case.
#
# Rules (from archive/go/internal/domain/context.go validateCombination):
#   1. GLOBAL + owner_user_id != ""  -> "global must not have owner"
#   2. GLOBAL + project_id != ""     -> "global must not have project"
#   3. USER   + owner_user_id == ""  -> "user scope requires owner"
#   4. USER   + project_id != ""     -> "user scope must not have project
#                                        (use project scope)"
#   5. PROJECT + project_id == ""    -> "project scope requires project_id"
#   6. unknown scope                  -> "unknown scope %q"
#   7. MEMORY + scope != PROJECT     -> "memory kind requires project scope"
#   8. MEMORY + source not in (AGENT, SYNC)
#                                    -> "memory kind requires source=agent or sync"
#   9. CONVERSATION_MSG + scope != PROJECT
#                                    -> "conversation_msg kind requires project scope"
#  10. CONVERSATION_MSG + source not in (AGENT, SYNC)
#                                    -> "conversation_msg kind requires source=agent or sync"
# ---------------------------------------------------------------------------


def test_validate_global_owner_negative():
    with pytest.raises(ItemValidationError, match="global must not have owner"):
        new_context_item(
            Scope.GLOBAL,
            Kind.NOTE,
            Source.MANUAL,
            NewItemParams(owner_user_id="u1"),
        )


def test_validate_global_owner_positive():
    # No owner + global: OK
    new_context_item(Scope.GLOBAL, Kind.NOTE, Source.MANUAL, NewItemParams())


def test_validate_global_project_negative():
    with pytest.raises(ItemValidationError, match="global must not have project"):
        new_context_item(
            Scope.GLOBAL,
            Kind.NOTE,
            Source.MANUAL,
            NewItemParams(project_id="p1"),
        )


def test_validate_user_owner_negative():
    with pytest.raises(ItemValidationError, match="user scope requires owner"):
        new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, NewItemParams())


def test_validate_user_owner_positive():
    new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, NewItemParams(owner_user_id="u1"))


def test_validate_user_project_negative():
    with pytest.raises(ItemValidationError, match="user scope must not have project"):
        new_context_item(
            Scope.USER,
            Kind.NOTE,
            Source.MANUAL,
            NewItemParams(owner_user_id="u1", project_id="p1"),
        )


def test_validate_project_id_negative():
    with pytest.raises(ItemValidationError, match="project scope requires project_id"):
        new_context_item(Scope.PROJECT, Kind.NOTE, Source.MANUAL, NewItemParams())


def test_validate_project_id_positive():
    new_context_item(
        Scope.PROJECT,
        Kind.NOTE,
        Source.MANUAL,
        NewItemParams(project_id="p1"),
    )


def test_validate_unknown_scope_negative():
    """Mirrors Go's default branch — unknown scope value."""
    with pytest.raises(ItemValidationError, match="unknown scope"):
        # Bypass type system to inject an unknown scope string.
        new_context_item(  # type: ignore[arg-type]
            "outer_space",
            Kind.NOTE,
            Source.MANUAL,
            NewItemParams(),
        )


def test_validate_memory_scope_negative():
    with pytest.raises(ItemValidationError, match="memory kind requires project scope"):
        new_context_item(
            Scope.GLOBAL,
            Kind.MEMORY,
            Source.AGENT,
            NewItemParams(),
        )


def test_validate_memory_scope_positive():
    new_context_item(
        Scope.PROJECT,
        Kind.MEMORY,
        Source.AGENT,
        NewItemParams(project_id="p1"),
    )


def test_validate_memory_source_negative():
    with pytest.raises(ItemValidationError, match="memory kind requires source=agent or sync"):
        new_context_item(
            Scope.PROJECT,
            Kind.MEMORY,
            Source.MANUAL,
            NewItemParams(project_id="p1"),
        )


def test_validate_memory_source_sync_positive():
    """MEMORY + SYNC source is legal (Go allows agent OR sync)."""
    new_context_item(
        Scope.PROJECT,
        Kind.MEMORY,
        Source.SYNC,
        NewItemParams(project_id="p1"),
    )


def test_validate_conversation_msg_scope_negative():
    with pytest.raises(
        ItemValidationError,
        match="conversation_msg kind requires project scope",
    ):
        new_context_item(
            Scope.GLOBAL,
            Kind.CONVERSATION_MSG,
            Source.AGENT,
            NewItemParams(),
        )


def test_validate_conversation_msg_scope_positive():
    new_context_item(
        Scope.PROJECT,
        Kind.CONVERSATION_MSG,
        Source.AGENT,
        NewItemParams(project_id="p1"),
    )


def test_validate_conversation_msg_source_negative():
    with pytest.raises(
        ItemValidationError,
        match="conversation_msg kind requires source=agent or sync",
    ):
        new_context_item(
            Scope.PROJECT,
            Kind.CONVERSATION_MSG,
            Source.IMPORT,
            NewItemParams(project_id="p1"),
        )


def test_validate_conversation_msg_source_sync_positive():
    new_context_item(
        Scope.PROJECT,
        Kind.CONVERSATION_MSG,
        Source.SYNC,
        NewItemParams(project_id="p1"),
    )


# ---------------------------------------------------------------------------
# count_words — preserves Go's algorithm verbatim (incl. CJK per-ideograph).
# ---------------------------------------------------------------------------


def test_count_words_ascii():
    assert count_words("hello world") == 2
    assert count_words("one two three four") == 4


def test_count_words_empty():
    assert count_words("") == 0


def test_count_words_whitespace_only():
    assert count_words("   \t\n  ") == 0


def test_count_words_leading_trailing_whitespace():
    assert count_words("  hello  world  ") == 2


def test_count_words_punctuation_does_not_split():
    """Go treats anything non-space, non-CJK as part of a word, so
    'hello,world' is one word."""
    assert count_words("hello,world") == 1


def test_count_words_cjk_per_ideograph():
    """Each CJK ideograph counts as one word (Go parity — NOT proper
    word segmentation). '你好世界' is one Chinese word but counts as 4
    per Go's algorithm."""
    assert count_words("你好世界") == 4


def test_count_words_mixed_ascii_cjk():
    """ASCII word ends the inWord state at the CJK char; each CJK char
    is its own word."""
    assert count_words("hello 世界") == 3  # 'hello' + '世' + '界'


def test_count_words_hangul():
    """Hangul syllables are in the CJK ranges Go recognizes."""
    assert count_words("안녕하세요") == 5


def test_count_words_hiragana_katakana():
    assert count_words("こんにちは") == 5
    assert count_words("コンニチハ") == 5


# ---------------------------------------------------------------------------
# new_project
# ---------------------------------------------------------------------------


def test_new_project_happy():
    p = new_project("my-proj", "/tmp/x", "desc")
    assert p.name == "my-proj"
    assert p.path == "/tmp/x"
    assert p.description == "desc"
    parsed = uuid.UUID(p.id)
    assert parsed.version == 7
    assert p.created_at == p.updated_at
    assert p.created_at > 0


def test_new_project_empty_name_raises():
    with pytest.raises(ItemValidationError, match="project name required"):
        new_project("", "/x", "")
