<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Liteset documentation

Public documentation site for **Liteset**, the async Litestar/ASGI port of Apache Superset 6.0.0. Built with [Docusaurus 3](https://docusaurus.io/).

## Local development

```bash
yarn install
yarn start          # English (default)
yarn start --locale ru   # Russian
```

The `_init` script bundles `src/intro_header.txt` together with the project root `README.md` into `docs/intro.md` before Docusaurus boots, so the home page always reflects the current README.

## Build

```bash
yarn build          # builds en + ru
yarn serve          # serve the built site locally
```

## Adding translations

Translation files live under `i18n/<locale>/`. Run `yarn write-translations --locale ru` after touching `<Translate>` tags or `translate({...})` calls in `src/` to refresh the JSON catalogue.

For docs content, mirror the `docs/` tree under `i18n/<locale>/docusaurus-plugin-content-docs/current/` and translate file by file.
