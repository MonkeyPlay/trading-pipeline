# License

## MIT License

Copyright (c) 2026 Andreas Vrhovsek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Third-party notices

The MIT grant above covers the original code in this repository. Some files are
derived from, or vendored from, third-party work and carry their own terms. Those
terms govern those files and are **not** superseded by the MIT license above.

### TradingView Lightweight Charts™ — Apache-2.0

`dashboard/components/lib/lightweight-charts.standalone.js` is a vendored, unmodified
build of TradingView's Lightweight Charts v5.2.1.

> Copyright (c) 2026 TradingView, Inc.
> Licensed under the Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0

"TradingView" and "Lightweight Charts" are trademarks of TradingView, Inc. Nothing in
this project is affiliated with or endorsed by TradingView.

### "TEMA & Session Levels" indicator — MPL-2.0

`indicator/TEMA & Session Levels.pine` and its Python port `indicator/tema_session.py`
are derived from a Pine Script® indicator by **© Ununseptium**, licensed under the
Mozilla Public License 2.0.

> This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
> If a copy of the MPL was not distributed with this file, you can obtain one at
> https://mozilla.org/MPL/2.0/

MPL-2.0 is file-level copyleft. These two files remain under MPL-2.0 even though the
rest of the project is MIT: if you distribute a modified version of either file, you
must make that file's source available under the MPL. The MPL explicitly permits
combining these files with the MIT-licensed code around them, so the project as a
whole can still be used and redistributed — only those files carry the obligation.

### "Auto Anchored VWAP [v6]" indicator — no stated license

`indicator/Auto Anchored VWAP.pine` was obtained as a Pine v6 script that carries no
copyright, author, or license notice of its own, and `indicator/auto_anchored_vwap.py`
is a Python port of it. Its provenance therefore cannot be verified, and no license
grant for it is claimed here.

It is retained for personal research reference. If you intend to redistribute this
project or use it commercially, establish the origin and licensing of that script
first, or replace the port with an independent implementation. If you are the author
and want it attributed differently or removed, please open an issue.

---

## No warranty, and not financial advice

This is research tooling, not trading infrastructure. Nothing in it places orders, and
nothing it produces is investment advice or a recommendation to trade. Market data
retrieved through Interactive Brokers remains subject to your agreements with IBKR and
the relevant exchanges; this license grants no rights to that data. You are solely
responsible for any trading decisions and any losses arising from them.
