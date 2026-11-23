from thaw.selection import CHECKED, PARTIAL, UNCHECKED, GlobSelection, TreeSelection

B = "P/"


def test_tick_parent_untick_child():
    s = TreeSelection(B)
    s.toggle(B)
    assert s.state(B) == CHECKED
    s.toggle(B + "ANALYSIS/")
    assert s.state(B) == PARTIAL
    assert s.state(B + "ANALYSIS/") == UNCHECKED
    assert s.is_selected(B + "DOCS/a.pdf")
    assert not s.is_selected(B + "ANALYSIS/a.tif")
    assert s.roots == [B]


def test_retick_child_simplifies_rules():
    s = TreeSelection(B)
    s.toggle(B)
    s.toggle(B + "A/")
    s.toggle(B + "A/")
    assert s.rules == {B: True}
    assert s.state(B) == CHECKED


def test_toggling_partial_parent_selects_all():
    s = TreeSelection(B)
    s.toggle(B + "A/")
    assert s.state(B) == PARTIAL
    s.toggle(B)
    assert s.rules == {B: True}


def test_file_rule_does_not_cover_similar_names():
    s = TreeSelection(B)
    s.toggle(B + "out.tif")
    assert s.is_selected(B + "out.tif")
    assert not s.is_selected(B + "out.tif.aux.xml")
    s.toggle(B)  # partial -> select all
    s.toggle(B + "out.tif")
    assert not s.is_selected(B + "out.tif")
    assert s.is_selected(B + "out.tif.aux.xml")


def test_nested_include_inside_excluded():
    s = TreeSelection(B)
    s.toggle(B)
    s.toggle(B + "A/")
    s.toggle(B + "A/keep/")
    assert s.is_selected(B + "A/keep/x")
    assert not s.is_selected(B + "A/drop/x")
    assert s.state(B + "A/") == PARTIAL
    assert s.roots == [B]


def test_glob_first_level():
    g = GlobSelection(B, exclude=["ANALYSIS"])
    assert g.is_selected(B + "DOCS/a/b.pdf")
    assert not g.is_selected(B + "ANALYSIS/x.tif")
    assert g.is_selected(B + "ANALYSIS_OLD/x.tif")
    assert not g.is_selected("Q/other")
    g = GlobSelection(B, include=["DOC*", "FINAL*"])
    assert g.is_selected(B + "DOCS/x") and g.is_selected(B + "FINAL_DELIVERABLES/y")
    assert not g.is_selected(B + "IMAGES/z")


def test_glob_deep():
    g = GlobSelection(B, exclude=["*.aux.xml"], deep=True)
    assert not g.is_selected(B + "F/out.tif.aux.xml")
    assert g.is_selected(B + "F/out.tif")
