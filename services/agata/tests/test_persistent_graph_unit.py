from app.memory.persistent_graph import (
    ENTITY_TYPE_MAPPING,
    GRAPH_ELIGIBLE_PREDICATES,
    RELATION_TYPE_MAPPING,
    normalize_entity_value,
)


def test_graph_entity_normalization_is_conservative_and_bounded():
    assert normalize_entity_value(" Coffee  ") == normalize_entity_value("coffee")
    assert normalize_entity_value("New York") != normalize_entity_value("NYC")
    assert normalize_entity_value("\x00") is None
    assert normalize_entity_value("x" * 513) is None


def test_graph_predicate_mapping_is_finite_and_never_infers_edges():
    assert set(ENTITY_TYPE_MAPPING) == GRAPH_ELIGIBLE_PREDICATES
    assert set(RELATION_TYPE_MAPPING) == GRAPH_ELIGIBLE_PREDICATES
    assert "future_unknown_predicate" not in GRAPH_ELIGIBLE_PREDICATES
    assert RELATION_TYPE_MAPPING["works_at"] == "works_at"
