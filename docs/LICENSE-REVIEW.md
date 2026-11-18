# License and source attribution review

HALO Local retains its existing **LGPL-3.0-only** project declaration. The complete
LGPLv3 text is in [LICENSE](../LICENSE), with the incorporated GPLv3 text in
[COPYING.GPLv3](../COPYING.GPLv3). Upstream contributors retain their copyrights
and applicable license grants, including any option to use later versions.

## recsrmesh-cpp README discrepancy

Reviewed on 2026-11-13. The upstream history supplies an explicit licensing
statement that was missing from our initial review:

- The [README addition](https://github.com/oyvindkinsey/recsrmesh-cpp/commit/e92b3e7b05575f183091c51716413c1149f50f2e)
  dates to February 23, 2026 and labels the project GPL-3.0-or-later.
- On October 18, 2026, maintainer `oyvindkinsey` authored and committed
  [726c2b2](https://github.com/oyvindkinsey/recsrmesh-cpp/commit/726c2b28420eb336366a2ec1036efc1dd2a6bc60),
  adding the LICENSE file. Its commit message explicitly describes the license
  as **LGPL-3.0-or-later**. The added file contains LGPLv3.
- The older README was subsequently merged through
  [b27df19](https://github.com/oyvindkinsey/recsrmesh-cpp/commit/b27df19b55a19baa1b596297b86a66686790c478).
  At inspected revision `006e8428fe262829a80ac3d309d60be9b6bc8815`, its GPL wording
  remains, while LICENSE is still the file added by the licensing commit.

Our documented conclusion is that the README's GPL line is stale. The explicit
maintainer licensing commit and matching LICENSE support using the LGPL grant
for this port. The specific README discrepancy is resolved for this publication
review; this does not claim that upstream has corrected its README or that a
maintainer has answered a new inquiry. No upstream repository was modified.

Pinned evidence:

- [LICENSE at the licensing commit](https://github.com/oyvindkinsey/recsrmesh-cpp/blob/726c2b28420eb336366a2ec1036efc1dd2a6bc60/LICENSE):
  Git blob `970cf6ae16ec1ed0f7d9413fc9bd093ff9c423c8`.
- [README at the inspected revision](https://github.com/oyvindkinsey/recsrmesh-cpp/blob/006e8428fe262829a80ac3d309d60be9b6bc8815/README.md):
  Git blob `51b6cc7d4eb6591aa56779e279700cf1ccc9b3bb`.

## Other upstream references

The November 13 review found LGPLv3 LICENSE files in
[avionmesh-cpp](https://github.com/oyvindkinsey/avionmesh-cpp/blob/main/LICENSE) and
[esphome-avionmesh](https://github.com/oyvindkinsey/esphome-avionmesh/blob/main/LICENSE).
Their README labels were LGPL-3.0-or-later and LGPL-3.0, respectively. All three
upstream LICENSE responses had the same Git blob listed above.

See [NOTICE](../NOTICE) and [provenance](PROVENANCE.json) for source attribution
and inspected references. Local ESP extensions are identified as local work,
not represented as upstream releases. Cryptography, Bleak, aiomqtt and aiohttp
are separately installed packages; their distributions retain their own notices.
