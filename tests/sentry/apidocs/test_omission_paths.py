from __future__ import annotations

from typing import Any

import pytest
from rest_framework import serializers

from sentry.apidocs.omission_apply import (
    DEPRECATE,
    OmissionError,
    apply_path,
    apply_to_operations,
    parents_by_component,
    resolved_declarations,
    strip_choice_description,
)
from sentry.apidocs.omission_paths import FIELD, VALUE, PathError, parse_path, resolve
from sentry.apidocs.omissions import (
    DEPRECATION_REASONS_OVERRIDE,
    OMISSION_REASONS_OVERRIDE,
    sentry_schema_serializer,
)


class Inner(serializers.Serializer):
    token = serializers.CharField()
    label = serializers.CharField()


class Shape(serializers.Serializer):
    name = serializers.CharField()
    data_source = serializers.ChoiceField(choices=("discover", "events", "spans"))
    config = Inner()
    items = serializers.ListField(child=Inner())
    sort = serializers.ListField(child=serializers.ChoiceField(choices=("-age", "age")))
    options = serializers.JSONField()


# --- parsing and resolution ---


def test_a_flat_path_names_a_field() -> None:
    assert resolve(Shape(), "name").kind == FIELD


def test_a_choice_field_resolves_the_next_segment_as_a_value() -> None:
    assert resolve(Shape(), "data_source.discover").kind == VALUE


def test_a_nested_serializer_resolves_the_next_segment_as_a_field() -> None:
    assert resolve(Shape(), "config.token").kind == FIELD


def test_a_list_field_descends_into_its_child() -> None:
    assert resolve(Shape(), "items.token").kind == FIELD


def test_a_list_of_choices_resolves_a_value() -> None:
    assert resolve(Shape(), "sort.-age").kind == VALUE


@pytest.mark.parametrize(
    "path,expected",
    (
        ("name.nope", "has no addressable parts"),
        ("data_source.nope", "does not accept"),
        ("data_source.discover.deeper", "has no parts to address"),
        ("options.anything", "dynamic mapping"),
        ("config.nope", "names nothing"),
        ("nope", "names nothing"),
    ),
)
def test_an_unresolvable_path_is_reported(path: str, expected: str) -> None:
    with pytest.raises(PathError) as exc:
        resolve(Shape(), path)
    assert expected in str(exc.value)


@pytest.mark.parametrize("path", ("", "  ", "a..b", ".a", "a."))
def test_a_malformed_path_is_rejected(path: str) -> None:
    with pytest.raises(PathError):
        parse_path(path)


def test_choices_come_from_the_built_field_not_source() -> None:
    computed = [value.upper() for value in ("a", "b")]

    class Computed(serializers.Serializer):
        kind = serializers.ChoiceField(choices=computed)

    assert resolve(Computed(), "kind.A").kind == VALUE


# --- the two verbs on the decorator ---


def test_both_verbs_record_their_reasons() -> None:
    @sentry_schema_serializer(
        omit_from_public_schema={"data_source.discover": "Deprecated; use events."},
        deprecate={"name": "Use slug."},
    )
    class Declared(Shape):
        pass

    from drf_spectacular.drainage import get_override

    assert get_override(Declared, OMISSION_REASONS_OVERRIDE) == {
        "data_source.discover": "Deprecated; use events."
    }
    assert get_override(Declared, DEPRECATION_REASONS_OVERRIDE) == {"name": "Use slug."}
    # A one-segment path is handled natively; a deeper one is not.
    assert get_override(Declared, "deprecate_fields") == ["name"]
    assert get_override(Declared, "exclude_fields", []) == []


def test_a_path_cannot_be_both_withheld_and_deprecated() -> None:
    with pytest.raises(ValueError) as exc:
        sentry_schema_serializer(omit_from_public_schema={"name": "a"}, deprecate={"name": "b"})
    assert "both withheld and deprecated" in str(exc.value)


def test_deprecation_requires_a_reason() -> None:
    with pytest.raises(ValueError) as exc:
        sentry_schema_serializer(deprecate={"name": "   "})
    assert "needs a reason" in str(exc.value)


def test_the_older_list_form_still_works() -> None:
    @sentry_schema_serializer(deprecate_fields=["name"])
    class Legacy(Shape):
        pass

    from drf_spectacular.drainage import get_override

    assert get_override(Legacy, "deprecate_fields") == ["name"]


def test_only_deep_paths_need_postprocessing() -> None:
    resolved = resolved_declarations(
        Shape(), {"name": "r", "data_source.discover": "r", "config.token": "r"}
    )
    assert sorted(resolved) == ["config.token", "data_source.discover"]


# --- applying to a generated schema ---


def _schemas() -> dict[str, Any]:
    return {
        "Parent": {
            "properties": {
                "name": {"type": "string"},
                "data_source": {"type": "string", "enum": ["discover", "events", "spans"]},
                "shared": {"$ref": "#/components/schemas/Shared"},
            }
        },
        "Other": {"properties": {"shared": {"$ref": "#/components/schemas/Shared"}}},
        "Lonely": {"properties": {"only": {"$ref": "#/components/schemas/Owned"}}},
        "Shared": {"properties": {"token": {"type": "string"}, "label": {"type": "string"}}},
        "Owned": {"properties": {"token": {"type": "string"}}},
    }


def test_withholding_a_choice_leaves_the_others() -> None:
    schemas = _schemas()
    apply_path(schemas, "Parent", "data_source.discover", VALUE, {}, {})
    assert schemas["Parent"]["properties"]["data_source"]["enum"] == ["events", "spans"]


def test_withholding_a_field_of_a_singly_used_shape() -> None:
    schemas = _schemas()
    parents = parents_by_component(schemas)
    apply_path(schemas, "Lonely", "only.token", FIELD, {}, parents)
    assert "token" not in schemas["Owned"]["properties"]


def test_a_shared_shape_needs_every_parent_to_agree() -> None:
    schemas = _schemas()
    parents = parents_by_component(schemas)
    with pytest.raises(OmissionError) as exc:
        apply_path(schemas, "Parent", "shared.token", FIELD, {}, parents)
    assert "Other" in str(exc.value)
    assert schemas["Shared"]["properties"]["token"] == {"type": "string"}


def test_unanimous_parents_may_withhold_from_a_shared_shape() -> None:
    schemas = _schemas()
    parents = parents_by_component(schemas)
    agreed = {"shared.token": {"Parent", "Other"}}
    apply_path(schemas, "Parent", "shared.token", FIELD, agreed, parents)
    assert "token" not in schemas["Shared"]["properties"]
    assert "label" in schemas["Shared"]["properties"]


def test_parents_are_found_through_a_ref() -> None:
    assert parents_by_component(_schemas())["Shared"] == {"Parent", "Other"}


def test_a_value_absent_from_the_generated_enum_is_reported() -> None:
    schemas = _schemas()
    with pytest.raises(OmissionError) as exc:
        apply_path(schemas, "Parent", "data_source.gone", VALUE, {}, {})
    assert "not in the generated enum" in str(exc.value)


def test_withholding_a_required_property_drops_it_from_required() -> None:
    schemas = {
        "Parent": {
            "properties": {"kept": {"type": "string"}, "gone": {"type": "string"}},
            "required": ["gone", "kept"],
        }
    }
    apply_path(schemas, "Parent", "gone", FIELD, {}, {})
    assert schemas["Parent"]["required"] == ["kept"]


def test_an_empty_required_list_is_removed_entirely() -> None:
    schemas = {"Parent": {"properties": {"gone": {"type": "string"}}, "required": ["gone"]}}
    apply_path(schemas, "Parent", "gone", FIELD, {}, {})
    assert "required" not in schemas["Parent"]


def test_deprecating_marks_rather_than_removes() -> None:
    schemas = {"Parent": {"properties": {"old": {"type": "string"}}}}
    apply_path(schemas, "Parent", "old", DEPRECATE, {}, {})
    assert schemas["Parent"]["properties"]["old"] == {"type": "string", "deprecated": True}


def test_deprecating_a_ref_property_keeps_the_marker_visible() -> None:
    schemas = {
        "Parent": {"properties": {"shared": {"$ref": "#/components/schemas/Shared"}}},
        "Shared": {"properties": {}},
    }
    apply_path(schemas, "Parent", "shared", DEPRECATE, {}, {})
    assert schemas["Parent"]["properties"]["shared"] == {
        "allOf": [{"$ref": "#/components/schemas/Shared"}],
        "deprecated": True,
    }


def _operation(names: list[str], enum: list[str] | None = None) -> dict[str, Any]:
    parameters = [{"name": n, "in": "query", "schema": {"type": "string"}} for n in names]
    if enum is not None:
        parameters[0]["schema"]["enum"] = list(enum)
    return {"/x/": {"get": {"parameters": parameters}}}


def test_a_value_is_withheld_from_an_exploded_query_parameter() -> None:
    paths = _operation(["data_source", "query"], ["discover", "events"])
    applied = apply_to_operations(paths, {"data_source", "query"}, "data_source.discover", VALUE)
    assert applied == 1
    assert paths["/x/"]["get"]["parameters"][0]["schema"]["enum"] == ["events"]


def test_an_operation_missing_a_field_is_not_matched() -> None:
    paths = _operation(["data_source"], ["discover", "events"])
    applied = apply_to_operations(paths, {"data_source", "query"}, "data_source.discover", VALUE)
    assert applied == 0
    assert paths["/x/"]["get"]["parameters"][0]["schema"]["enum"] == ["discover", "events"]


def test_withholding_the_default_value_says_so() -> None:
    class Defaulted(serializers.Serializer):
        kind = serializers.ChoiceField(choices=("a", "b"), default="a")

    resolved = resolve(Defaulted(), "kind.a")
    assert resolved.kind == VALUE
    assert resolved.withholds_default


def test_withholding_a_non_default_does_not_claim_the_default() -> None:
    class Defaulted(serializers.Serializer):
        kind = serializers.ChoiceField(choices=("a", "b"), default="a")

    assert not resolve(Defaulted(), "kind.b").withholds_default


def test_withholding_the_default_requires_the_parameter_and_drops_it() -> None:
    paths = {
        "/x/": {
            "get": {
                "parameters": [
                    {
                        "name": "kind",
                        "in": "query",
                        "schema": {"enum": ["a", "b"], "default": "a"},
                    }
                ]
            }
        }
    }
    apply_to_operations(paths, {"kind"}, "kind.a", VALUE, withholds_default=True)
    parameter = paths["/x/"]["get"]["parameters"][0]
    assert parameter["schema"] == {"enum": ["b"]}
    assert parameter["required"] is True


def test_withholding_a_non_default_value_is_allowed() -> None:
    class Defaulted(serializers.Serializer):
        kind = serializers.ChoiceField(choices=("a", "b"), default="a")

    assert resolve(Defaulted(), "kind.b").kind == VALUE


def test_a_withheld_value_is_stripped_from_the_description() -> None:
    holder = {"description": "Pick one.\n\n* `a` - A\n* `b` - B"}
    strip_choice_description(holder, "a")
    assert holder["description"] == "Pick one.\n\n* `b` - B"


def test_agreement_is_keyed_the_way_the_hook_records_it() -> None:
    schemas = _schemas()
    parents = parents_by_component(schemas)
    # The hook records {path: declaring components}, not {component:path: ...}.
    apply_path(schemas, "Parent", "shared.token", FIELD, {"shared.token": {"Other"}}, parents)
    assert "token" not in schemas["Shared"]["properties"]
