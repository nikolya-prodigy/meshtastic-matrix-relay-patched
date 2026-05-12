from mmrelay.matrix.fragments import (
    MESHTASTIC_TEXT_PAYLOAD_LIMIT_BYTES,
    get_message_fragmentation_config,
    split_text_for_meshtastic,
)


def _config(**overrides):
    settings = {
        "enabled": True,
        "max_payload_bytes": 80,
        "prefix_template": "[{index}/{total}] ",
    }
    settings.update(overrides)
    return {"meshtastic": {"message_fragmentation": settings}}


def test_fragmentation_disabled_returns_original_text():
    text = "Очень длинное сообщение " * 20

    assert split_text_for_meshtastic(text, {}) == [text]


def test_short_text_is_not_fragmented_when_enabled():
    text = "Короткое сообщение"

    assert split_text_for_meshtastic(text, _config()) == [text]


def test_long_cyrillic_text_is_split_on_utf8_byte_limit():
    text = (
        "Внимание! Возможна чрезвычайная ситуация. "
        "Проверьте окна, документы, воду и заряд телефона. "
        "Следите за официальными сообщениями. "
    ) * 3

    fragments = split_text_for_meshtastic(text, _config(max_payload_bytes=90))

    assert len(fragments) > 1
    assert fragments[0].startswith("[1/")
    assert fragments[-1].startswith(f"[{len(fragments)}/{len(fragments)}] ")
    assert all(len(fragment.encode("utf-8")) <= 90 for fragment in fragments)
    assert "чрезвычайная" in " ".join(fragments)


def test_payload_size_is_clamped_to_meshtastic_limit():
    normalized = get_message_fragmentation_config(
        _config(max_payload_bytes=MESHTASTIC_TEXT_PAYLOAD_LIMIT_BYTES + 100)
    )

    assert normalized["max_payload_bytes"] == MESHTASTIC_TEXT_PAYLOAD_LIMIT_BYTES


def test_invalid_prefix_template_falls_back_to_default():
    text = "0123456789 " * 20

    fragments = split_text_for_meshtastic(
        text,
        _config(max_payload_bytes=50, prefix_template="{missing} "),
    )

    assert fragments[0].startswith("[1/")
    assert all(len(fragment.encode("utf-8")) <= 50 for fragment in fragments)
