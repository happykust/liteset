/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

import type { Config } from '@docusaurus/types';
import type { Options, ThemeConfig } from '@docusaurus/preset-classic';
import { themes } from 'prism-react-renderer';

const { github: lightCodeTheme, vsDark: darkCodeTheme } = themes;

const LITESET_GITHUB = 'https://github.com/happykust/liteset';
const UPSTREAM_GITHUB = 'https://github.com/apache/superset';
const LITESET_VERSION = '6.0.0';

const config: Config = {
  title: 'Liteset',
  tagline:
    'Liteset — асинхронный порт Apache Superset на Litestar/ASGI с полной обратной совместимостью',
  url: 'https://liteset.happykust.dev',
  baseUrl: '/',
  onBrokenLinks: 'warn',
  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },
  favicon: '/img/favicon.ico',
  organizationName: 'happykust',
  projectName: 'liteset',
  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'ru'],
    localeConfigs: {
      en: {
        label: 'English',
        direction: 'ltr',
        htmlLang: 'en',
        path: 'en',
      },
      ru: {
        label: 'Русский',
        direction: 'ltr',
        htmlLang: 'ru',
        path: 'ru',
      },
    },
  },
  themes: ['@saucelabs/theme-github-codeblock', '@docusaurus/theme-mermaid'],
  plugins: [
    [
      'docusaurus-plugin-less',
      {
        lessOptions: {
          javascriptEnabled: true,
        },
      },
    ],
    [
      '@docusaurus/plugin-client-redirects',
      {
        fromExtensions: ['html', 'htm'],
        toExtensions: ['exe', 'zip'],
        redirects: [
          {
            to: '/docs/installation/docker-compose',
            from: '/installation.html',
          },
          {
            to: '/docs/intro',
            from: '/tutorials.html',
          },
          {
            to: '/docs/using-superset/creating-your-first-dashboard',
            from: '/admintutorial.html',
          },
          {
            to: '/docs/using-superset/creating-your-first-dashboard',
            from: '/usertutorial.html',
          },
          {
            to: '/docs/security/',
            from: '/security.html',
          },
          {
            to: '/docs/configuration/sql-templating',
            from: '/sqllab.html',
          },
          {
            to: '/docs/intro',
            from: '/gallery.html',
          },
          {
            to: '/docs/configuration/databases',
            from: '/druid.html',
          },
          {
            to: '/docs/faq',
            from: '/faq.html',
          },
          {
            to: '/docs/api',
            from: '/docs/rest-api',
          },
          {
            to: '/docs/configuration/alerts-reports',
            from: '/docs/installation/alerts-reports',
          },
          {
            to: '/docs/contributing/development',
            from: '/docs/contributing/hooks-and-linting',
          },
          {
            to: '/docs/intro',
            from: '/docs/roadmap',
          },
          {
            to: '/docs/contributing/',
            from: '/docs/contributing/contribution-guidelines',
          },
          {
            to: '/docs/configuration/databases',
            from: '/docs/databases/yugabyte/',
          },
          {
            to: '/docs/faq',
            from: '/docs/frequently-asked-questions',
          },
          {
            to: '/docs/installation/kubernetes',
            from: '/docs/installation/running-on-kubernetes/',
          },
          {
            to: '/docs/installation/kubernetes',
            from: '/docs/installation/',
          },
          {
            to: '/docs/installation/pypi',
            from: '/docs/installation/installing-superset-from-pypi/',
          },
          {
            to: '/docs/configuration/configuring-superset',
            from: '/docs/installation/configuring-superset/',
          },
          {
            to: '/docs/configuration/cache',
            from: '/docs/installation/cache/',
          },
          {
            to: '/docs/configuration/async-queries-celery',
            from: '/docs/installation/async-queries-celery/',
          },
          {
            to: '/docs/configuration/event-logging',
            from: '/docs/installation/event-logging/',
          },
          {
            to: '/docs/benchmarks/results',
            from: '/benchmarks',
          },
        ],
      },
    ],
  ],

  presets: [
    [
      '@docusaurus/preset-classic',
      {
        docs: {
          sidebarPath: require.resolve('./sidebars.js'),
          editUrl: ({ versionDocsDirPath, docPath, locale }) => {
            const localePath = locale === 'en' ? '' : `i18n/${locale}/docusaurus-plugin-content-docs/current/`;
            if (docPath === 'intro.md') {
              return `${LITESET_GITHUB}/edit/main/README.md`;
            }
            return `${LITESET_GITHUB}/edit/main/docs/${localePath}${versionDocsDirPath}/${docPath}`;
          },
        },
        blog: false,
        theme: {
          customCss: require.resolve('./src/styles/custom.css'),
        },
      } satisfies Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: false,
      respectPrefersColorScheme: true,
    },
    navbar: {
      logo: {
        alt: 'Liteset Logo',
        src: '/img/liteset-logo-horiz.svg',
        srcDark: '/img/liteset-logo-horiz-dark.svg',
      },
      items: [
        {
          label: 'Documentation',
          to: '/docs/intro',
          items: [
            {
              label: 'Getting Started',
              to: '/docs/intro',
            },
            {
              label: 'Quickstart',
              to: '/docs/quickstart',
            },
            {
              label: 'Benchmarks',
              to: '/docs/benchmarks/results',
            },
            {
              label: 'FAQ',
              to: '/docs/faq',
            },
          ],
        },
        {
          label: 'Benchmarks',
          to: '/docs/benchmarks/results',
        },
        {
          label: 'Community',
          to: '/community',
          items: [
            {
              label: 'Resources',
              href: '/community',
            },
            {
              label: 'GitHub',
              href: LITESET_GITHUB,
            },
            {
              label: 'Upstream (Apache Superset)',
              href: UPSTREAM_GITHUB,
            },
          ],
        },
        {
          type: 'localeDropdown',
          position: 'right',
        },
        {
          href: '/docs/intro',
          position: 'right',
          className: 'default-button-theme get-started-button',
          label: 'Get Started',
        },
        {
          href: LITESET_GITHUB,
          position: 'right',
          className: 'github-button',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      links: [],
      copyright: `
          <p>
            <strong>Liteset ${LITESET_VERSION}</strong> — асинхронный порт Apache Superset 6.0.0
          </p>
          <p>
            Copyright © ${new Date().getFullYear()} Liteset contributors.
            Distributed under the <a href="https://apache.org/licenses/LICENSE-2.0" target="_blank" rel="noreferrer">Apache License 2.0</a>.
          </p>
          <p><small>
            Liteset основан на <a href="${UPSTREAM_GITHUB}" target="_blank" rel="noreferrer">Apache Superset 6.0.0</a>.
            Apache, Apache Superset, Superset and the Superset logo are trademarks of
            <a href="https://www.apache.org/" target="_blank" rel="noreferrer">The Apache Software Foundation</a>.
            All other product or company names are trademarks of their respective holders.
          </small></p>
          <img class="footer__divider" src="/img/community/line.png" alt="Divider" />
          <p>
            <small>
              <a href="/docs/security/" rel="noreferrer">Security</a>&nbsp;|&nbsp;
              <a href="/docs/benchmarks/results">Benchmarks</a>&nbsp;|&nbsp;
              <a href="${LITESET_GITHUB}" target="_blank" rel="noreferrer">GitHub</a>&nbsp;|&nbsp;
              <a href="${UPSTREAM_GITHUB}" target="_blank" rel="noreferrer">Upstream</a>
            </small>
          </p>
          `,
    },
    prism: {
      theme: lightCodeTheme,
      darkTheme: darkCodeTheme,
    },
    docs: {
      sidebar: {
        hideable: true,
      },
    },
  } satisfies ThemeConfig,
  customFields: {
    litesetVersion: LITESET_VERSION,
    upstreamVersion: '6.0.0',
    litesetGithub: LITESET_GITHUB,
    upstreamGithub: UPSTREAM_GITHUB,
  },
};

export default config;
