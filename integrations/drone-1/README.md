# Patch for reesick/drone-1 (optional, recommended)

`0001-external-routes-and-replace.patch` changes `swarm_ops/engine.py` (12 lines) so the swarm viewer shows
DisruptionOps' ground units exactly as DisruptionOps has them:

* `external_resource_update` keeps the `route` DisruptionOps sends (local metres, at most 200 points). The viewer
  draws it as the unit's road line. The swarm never drives these units itself; their positions come from DisruptionOps.
* `"replace": true` marks the payload as DisruptionOps' whole ground picture. Units it no longer lists are removed
  from the map and from the incidents they were on.

Without the patch the link still works: units show at their real positions with DisruptionOps' ETA, but with no
route line, and a unit that leaves the map stays at its last position.

Apply in the drone-1 repo and push to `main` (Render redeploys it on its own):

```
git am 0001-external-routes-and-replace.patch
python -m unittest discover -s tests -p "test_*.py" -q    # 242 tests, all pass with the patch
git push origin main
```
