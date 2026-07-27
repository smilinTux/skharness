"""Tests for the resource-based concurrency autoscaler."""

from __future__ import annotations

from skharness.autocode import autoscale


def _res(monkeypatch, cores, ram, disk):
    monkeypatch.setattr(autoscale, "resources",
                        lambda: {"cores": cores, "ram_gb": ram, "disk_gb": disk})


def test_recommended_leaves_headroom_max_is_aggressive(monkeypatch):
    _res(monkeypatch, cores=8, ram=30, disk=60)
    # recommended: cpu 7, ram 30//3=10, disk 60//3=20 -> 7
    assert autoscale.recommended() == 7
    # max: cpu 8, ram 30//2=15, disk 60//2=30 -> 8
    assert autoscale.maximum() == 8


def test_resolve_modes(monkeypatch):
    _res(monkeypatch, cores=8, ram=30, disk=60)
    assert autoscale.resolve("min") == 1
    assert autoscale.resolve("recommended") == 7
    assert autoscale.resolve("auto") == 7               # alias
    assert autoscale.resolve("max") == 8
    assert autoscale.resolve(None) == 7                 # default
    assert autoscale.resolve("bogus") == 7              # unknown -> recommended


def test_resolve_clamps_to_hard_cap(monkeypatch):
    _res(monkeypatch, cores=8, ram=30, disk=60)
    assert autoscale.resolve("max", hard_cap=3) == 3            # cap wins over resources
    assert autoscale.resolve("recommended", hard_cap=3) == 3
    assert autoscale.resolve("min", hard_cap=3) == 1           # min unaffected


def test_resolve_explicit_int(monkeypatch):
    _res(monkeypatch, cores=8, ram=30, disk=60)
    assert autoscale.resolve("4", hard_cap=8) == 4
    assert autoscale.resolve(4, hard_cap=8) == 4               # int, not str
    assert autoscale.resolve("999", hard_cap=8) == 8          # clamped to resource max (8)
    assert autoscale.resolve("0", hard_cap=8) == 1            # never below 1


def test_scarce_resource_is_the_binding_constraint(monkeypatch):
    _res(monkeypatch, cores=16, ram=6, disk=100)              # RAM-starved
    assert autoscale.recommended() == 2                        # 6 // 3
    _res(monkeypatch, cores=16, ram=64, disk=5)               # disk-starved
    assert autoscale.recommended() == 1                        # 5 // 3


def test_never_below_one_on_a_tiny_host(monkeypatch):
    _res(monkeypatch, cores=1, ram=0.5, disk=0.5)
    assert autoscale.recommended() == 1
    assert autoscale.maximum() == 1
    assert autoscale.resolve("max") == 1


def test_hard_ceiling_caps_a_huge_host(monkeypatch):
    _res(monkeypatch, cores=128, ram=512, disk=4000)
    assert autoscale.maximum() == autoscale._HARD_CEIL         # absolute sanity ceiling
