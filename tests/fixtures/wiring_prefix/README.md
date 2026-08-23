# Pre-fix negative-control fixtures (card bb536f68)

This is a miniature source tree that faithfully reproduces the SHAPE of each of
the eight built-but-unwired mechanisms from epic 935d4b61, as they stood BEFORE
their individual fix cards landed. It is the detector's negative control: the
reachability detector, pointed here with `inventory.yaml`, must flag ALL EIGHT.
Finding fewer than eight means the detector is itself inert (the ninth instance).

Each module below carries a passing-unit-test illusion of function while having
no live-path caller, exactly the class the card targets. The mapping of fixture
-> real instance is documented inline and in `inventory.yaml`.
