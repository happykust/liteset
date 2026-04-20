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
import { useRef, useState, useEffect, JSX } from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Translate, { translate } from '@docusaurus/Translate';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import { Carousel } from 'antd';
import styled from '@emotion/styled';
import GitHubButton from 'react-github-btn';
import { mq } from '../utils';
import { Databases } from '../resources/data';
import SectionHeader from '../components/SectionHeader';
import BlurredSection from '../components/BlurredSection';
import BenchmarkCharts from '../components/BenchmarkCharts';
import '../styles/main.less';

const features = [
  {
    image: 'powerful-yet-easy.jpg',
    titleId: 'home.features.powerful.title',
    titleDefault: 'Powerful, but async',
    descriptionId: 'home.features.powerful.description',
    descriptionDefault:
      'Liteset preserves the no-code chart builder and SQL Lab from Apache Superset, but the entire web layer runs on a single ASGI event loop instead of pre-forked Flask workers.',
  },
  {
    image: 'modern-databases.jpg',
    titleId: 'home.features.databases.title',
    titleDefault: 'Modern databases, native async drivers',
    descriptionId: 'home.features.databases.description',
    descriptionDefault:
      'Postgres, MySQL, ClickHouse, and Trino use native async drivers (asyncpg, asyncmy, aiochclient, aiotrino). Other databases keep working through a sync-fallback wrapper.',
  },
  {
    image: 'modern-architecture.jpg',
    titleId: 'home.features.architecture.title',
    titleDefault: 'Clean async architecture',
    descriptionId: 'home.features.architecture.description',
    descriptionDefault:
      'Four layers — Controllers, Commands, DAOs, AsyncSession — built on Litestar, SQLAlchemy 2.0 and msgspec. No Flask, no synchronous I/O on the hot path.',
  },
  {
    image: 'rich-visualizations.jpg',
    titleId: 'home.features.compat.title',
    titleDefault: 'Drop-in compatibility',
    descriptionId: 'home.features.compat.description',
    descriptionDefault:
      'The metadata DB schema, REST API, WebSocket contract and SPA frontend are inherited 1:1 from Apache Superset 6.0.0. Stop Superset, start Liteset on the same database.',
  },
];

const StyledMain = styled('main')`
  text-align: center;
`;

const StyledTitleContainer = styled('div')`
  position: relative;
  padding: 130px 20px 0;
  margin-bottom: 160px;
  background-image: url('/img/grid-background.jpg');
  background-size: cover;
  ${mq[1]} {
    margin-bottom: 100px;
  }
  .info-container {
    position: relative;
    z-index: 4;
  }
  .superset-mark {
    ${mq[1]} {
      width: 140px;
    }
  }
  .info-text {
    font-size: 30px;
    line-height: 37px;
    max-width: 720px;
    margin: 24px auto 10px;
    color: var(--ifm-font-base-color-inverse);
    ${mq[1]} {
      font-size: 25px;
      line-height: 30px;
    }
  }
  .version-pill {
    display: inline-block;
    margin: 18px auto 0;
    padding: 6px 14px;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.4);
    color: var(--ifm-font-base-color-inverse);
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .github-section {
    margin-top: 9px;
    ${mq[1]} {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .github-button {
      margin: 5px;
      ${mq[1]} {
        transform: scale(1.25);
        margin: 8px;
        &:first-of-type {
          margin-top: 5px;
        }
        &:last-of-type {
          margin-bottom: 5px;
        }
      }
    }
  }
`;

const StyledButton = styled(Link)`
  border-radius: 10px;
  font-size: 20px;
  font-weight: bold;
  width: 170px;
  padding: 10px 0;
  margin: 15px auto 0;
  ${mq[1]} {
    font-size: 19px;
    width: 175px;
    padding: 10px 0;
  }
`;

const StyledScreenshotContainer = styled('div')`
  position: relative;
  display: inline-block;
  padding-top: 30px;
  margin-top: 25px;
  margin-bottom: -125px;
  max-width: 800px;
  ${mq[1]} {
    padding-top: 20px;
  }
  .screenshot {
    position: relative;
    z-index: 3;
    border-radius: 10px;
  }
  .screenshot-shadow-1 {
    position: absolute;
    top: 15px;
    left: 20px;
    width: calc(100% - 40px);
    height: calc(100% - 15px);
    background-color: #256b7c;
    border-radius: 10px;
    z-index: 2;
    ${mq[1]} {
      background-color: #335a64;
      top: 10px;
      left: 15px;
      width: calc(100% - 30px);
      height: calc(100% - 10px);
    }
  }
  .screenshot-shadow-2 {
    position: absolute;
    top: 0;
    left: 40px;
    width: calc(100% - 80px);
    height: 100%;
    background-color: #0d5262;
    border-radius: 10px;
    z-index: 1;
    ${mq[1]} {
      background-color: #1f4048;
      left: 30px;
      width: calc(100% - 60px);
    }
  }
  .screenshotBlur {
    display: none;
    background-color: #173036;
    filter: blur(45px);
    position: absolute;
    bottom: 0;
    left: 50%;
    width: 100%;
    padding-top: 100%;
    border-radius: 50%;
    transform: translate3d(-50%, 0, 0);
    opacity: 0.3;
    ${mq[1]} {
      display: block;
    }
  }
`;

const StyledFeaturesList = styled('ul')`
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  width: 100%;
  max-width: 1170px;
  margin: 15px auto 0;
  padding: 0 20px;
  ${mq[1]} {
    grid-template-columns: repeat(1, minmax(0, 1fr));
  }
  .item {
    text-align: left;
    border: 1px solid var(--ifm-border-color);
    border-radius: 10px;
    overflow: hidden;
    display: flex;
    align-items: flex-start;
    padding: 20px;
    ${mq[1]} {
      flex-direction: column;
      align-items: center;
      text-align: center;
      padding: 35px;
    }
    .image {
      flex-shrink: 0;
      margin-right: 20px;
      width: 140px;
      text-align: center;
      ${mq[1]} {
        width: 115px;
      }
    }
    .title {
      font-size: 24px;
      margin: 10px 0 0;
      ${mq[1]} {
        font-size: 23px;
        margin-top: 20px;
      }
    }
    .description {
      font-size: 17px;
      line-height: 23px;
      margin: 5px 0 0;
      ${mq[1]} {
        font-size: 16px;
        margin-top: 10px;
      }
    }
  }
`;

const StyledSliderSection = styled('div')`
  position: relative;
  padding: 60px 20px;
  ${mq[1]} {
    padding-top: 0;
    padding-bottom: 50px;
  }
  &::before {
    content: '';
    display: block;
    width: 100%;
    height: calc(100% - 320px);
    position: absolute;
    top: 0;
    left: 0;
    background-image: url('/img/grid-background.jpg');
    background-size: cover;
    z-index: -1;
    ${mq[1]} {
      height: 100%;
    }
  }
  .toggleBtns {
    display: flex;
    justify-content: space-between;
    list-style: none;
    max-width: 870px;
    width: 100%;
    margin: 0 auto 20px;
    padding: 0;
    ${mq[1]} {
      flex-direction: column;
      text-align: left;
      max-width: 140px;
      gap: 10px;
      margin-top: 15px;
      margin-bottom: 40px;
    }
    .toggle {
      font-size: 24px;
      color: #b4c0c7;
      position: relative;
      padding-left: 32px;
      cursor: pointer;
      ${mq[1]} {
        font-size: 17px;
        font-weight: bold;
        padding-left: 22px;
      }
      &::before {
        content: '';
        display: block;
        width: 12px;
        height: 12px;
        border-radius: 50%;
        background-color: #457f8d;
        position: absolute;
        top: 50%;
        left: 0;
        transform: translate3d(0, -50%, 0);
        ${mq[1]} {
          width: 8px;
          height: 8px;
        }
      }
      &.active {
        font-weight: 700;
        color: var(--ifm-font-base-color-inverse);
      }
      &.active::before {
        background-color: var(--ifm-color-primary);
      }
    }
  }
  .slide {
    max-width: 920px;
    & > p {
      max-width: 560px;
      margin: 0 auto;
      font-size: 24px;
      line-height: 32px;
      color: var(--ifm-font-base-color-inverse);
      margin-bottom: 45px;
      ${mq[1]} {
        font-size: 17px;
        line-height: 23px;
      }
    }
  }
  video {
    width: 100%;
    max-width: 920px;
    margin-top: 10px;
    border-radius: 10px;
    ${mq[1]} {
      border-radius: 5px;
    }
  }
`;

const StyledKeyFeatures = styled('div')`
  margin-top: 50px;
  & > h3 {
    font-size: 30px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 30px;
    max-width: 960px;
    margin: 30px auto 0;
    padding: 0 20px;
    text-align: left;
    ${mq[1]} {
      grid-template-columns: repeat(1, minmax(0, 1fr));
    }
    & > .item {
      display: flex;
      font-size: 17px;
      ${mq[1]} {
        font-size: 15px;
      }
      & > img {
        width: 20px;
        height: 20px;
        flex-shrink: 0;
        margin-right: 12px;
        margin-top: 4px;
        ${mq[1]} {
          width: 18px;
          height: 18px;
          margin-top: 2px;
        }
      }
    }
  }
`;

const StyledIntegrations = styled('div')`
  padding: 0 20px;
  .database-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 14px;
    max-width: 1160px;
    margin: 25px auto 0;
    ${mq[1]} {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    ${mq[0]} {
      grid-template-columns: repeat(1, minmax(0, 1fr));
    }
    & > .item {
      border: 1px solid var(--ifm-border-color);
      border-radius: 10px;
      overflow: hidden;
      height: 120px;
      padding: 25px;
      display: flex;
      align-items: center;
      justify-content: center;
      & > a {
        height: 100%;
      }
      & img {
        height: 100%;
        object-fit: contain;
      }
    }
  }
  .database-sub {
    display: block;
    text-align: center;
    font-size: 17px;
    margin-top: 50px;
  }
`;

const StyledBenchmarksSection = styled('div')`
  padding: 0 20px;
  .benchmark-cta {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    margin-top: 32px;
    padding: 12px 24px;
    border-radius: 10px;
    font-size: 17px;
    font-weight: 600;
    background-color: var(--ifm-color-primary);
    color: #fff;
    text-decoration: none;
    transition: filter 0.2s ease;
    &:hover {
      filter: brightness(1.1);
      text-decoration: none;
      color: #fff;
    }
  }
`;

export default function Home(): JSX.Element {
  const slider = useRef(null);
  const { siteConfig } = useDocusaurusContext();
  const litesetGithub =
    (siteConfig.customFields?.litesetGithub as string) ??
    'https://github.com/happykust/liteset';
  const upstreamGithub =
    (siteConfig.customFields?.upstreamGithub as string) ??
    'https://github.com/apache/superset';
  const litesetVersion =
    (siteConfig.customFields?.litesetVersion as string) ?? '6.0.0';

  const [slideIndex, setSlideIndex] = useState(0);

  const onChange = (current, next) => {
    setSlideIndex(next);
  };

  const changeToDark = () => {
    const navbar = document.body.querySelector('.navbar');
    const logo = document.body.querySelector('.navbar__logo img');
    if (!navbar || !logo) return;
    navbar.classList.add('navbar--dark');
    logo.setAttribute('src', '/img/superset-logo-horiz-dark.svg');
  };

  const changeToLight = () => {
    const navbar = document.body.querySelector('.navbar');
    const logo = document.body.querySelector('.navbar__logo img');
    if (!navbar || !logo) return;
    navbar.classList.remove('navbar--dark');
    logo.setAttribute('src', '/img/superset-logo-horiz.svg');
  };

  // Set up dark <-> light navbar change
  useEffect(() => {
    changeToDark();

    const navbarToggle = document.body.querySelector('.navbar__toggle');
    const onToggleClick = () => changeToLight();
    if (navbarToggle) {
      navbarToggle.addEventListener('click', onToggleClick);
    }

    const scrollListener = () => {
      if (window.scrollY > 0) {
        changeToLight();
      } else {
        changeToDark();
      }
    };

    window.addEventListener('scroll', scrollListener);

    return () => {
      window.removeEventListener('scroll', scrollListener);
      if (navbarToggle) {
        navbarToggle.removeEventListener('click', onToggleClick);
      }
      changeToLight();
    };
  }, []);

  return (
    <Layout
      title={translate({
        id: 'home.meta.title',
        message: 'Welcome',
      })}
      description={translate({
        id: 'home.meta.description',
        message:
          'Liteset — async port of Apache Superset on Litestar/ASGI with full backward compatibility',
      })}
      wrapperClassName="under-navbar"
    >
      <StyledMain>
        <StyledTitleContainer>
          <div className="info-container">
            <img
              className="superset-mark"
              src="/img/superset-mark-dark.svg"
              alt="Liteset mark"
            />
            <div className="info-text">
              <Translate id="home.hero.tagline">
                Liteset is an async port of Apache Superset built on
                Litestar/ASGI — same dashboards, datasets, users and roles, a
                completely new web layer.
              </Translate>
            </div>
            <div>
              <span className="version-pill">
                <Translate
                  id="home.hero.versionPill"
                  values={{ version: litesetVersion }}
                >
                  {'Liteset {version} · based on Apache Superset 6.0.0'}
                </Translate>
              </span>
            </div>
            <img src="/img/community/line.png" alt="line" />
            <div className="github-section">
              <span className="github-button">
                <GitHubButton
                  href={litesetGithub}
                  data-size="large"
                  data-show-count="true"
                  aria-label="Star happykust/liteset on GitHub"
                >
                  Star
                </GitHubButton>
              </span>
              <span className="github-button">
                <GitHubButton
                  href={upstreamGithub}
                  data-size="large"
                  data-show-count="true"
                  aria-label="Star apache/superset on GitHub (upstream)"
                >
                  Upstream
                </GitHubButton>
              </span>
              <span className="github-button">
                <GitHubButton
                  href={`${litesetGithub}/fork`}
                  data-size="large"
                  data-show-count="true"
                  aria-label="Fork happykust/liteset on GitHub"
                >
                  Fork
                </GitHubButton>
              </span>
            </div>
            <img src="/img/community/line.png" alt="line" />
            <StyledButton className="default-button-theme" href="/docs/intro">
              <Translate id="home.hero.cta">Get Started</Translate>
            </StyledButton>
          </div>
          <StyledScreenshotContainer>
            <img
              className="screenshot"
              src="/img/hero-screenshot.jpg"
              alt="hero-screenshot"
            />
            <div className="screenshot-shadow-1"></div>
            <div className="screenshot-shadow-2"></div>
            <div className="screenshotBlur"></div>
          </StyledScreenshotContainer>
        </StyledTitleContainer>
        <BlurredSection>
          <SectionHeader
            level="h2"
            title={translate({
              id: 'home.overview.title',
              message: 'Overview',
            })}
            subtitle={translate({
              id: 'home.overview.subtitle',
              message:
                'Liteset keeps every familiar feature of Apache Superset and replaces the synchronous Flask backend with a single-loop ASGI server. Lower memory footprint, higher concurrency, identical UX.',
            })}
          />
          <StyledFeaturesList>
            {features.map(
              ({
                image,
                titleId,
                titleDefault,
                descriptionId,
                descriptionDefault,
              }) => (
                <li className="item" key={titleId}>
                  <div className="image">
                    <img src={`/img/features/${image}`} />
                  </div>
                  <div className="content">
                    <h4 className="title">
                      <Translate id={titleId}>{titleDefault}</Translate>
                    </h4>
                    <p className="description">
                      <Translate id={descriptionId}>
                        {descriptionDefault}
                      </Translate>
                    </p>
                  </div>
                </li>
              ),
            )}
          </StyledFeaturesList>
        </BlurredSection>
        <BlurredSection>
          <StyledSliderSection>
            <SectionHeader
              level="h2"
              title={translate({
                id: 'home.selfServe.title',
                message: 'Self-serve analytics for anyone',
              })}
              dark
            />
            <ul className="toggleBtns">
              <li
                className={`toggle ${slideIndex === 0 ? 'active' : null}`}
                onClick={() => slider.current.goTo(0)}
                role="button"
              >
                <Translate id="home.selfServe.tab.dashboards">
                  Dashboards
                </Translate>
              </li>
              <li
                className={`toggle ${slideIndex === 1 ? 'active' : null}`}
                onClick={() => slider.current.goTo(1)}
                role="button"
              >
                <Translate id="home.selfServe.tab.chartBuilder">
                  Chart Builder
                </Translate>
              </li>
              <li
                className={`toggle ${slideIndex === 2 ? 'active' : null}`}
                onClick={() => slider.current.goTo(2)}
                role="button"
              >
                <Translate id="home.selfServe.tab.sqlLab">SQL Lab</Translate>
              </li>
              <li
                className={`toggle ${slideIndex === 3 ? 'active' : null}`}
                onClick={() => slider.current.goTo(3)}
                role="button"
              >
                <Translate id="home.selfServe.tab.datasets">
                  Datasets
                </Translate>
              </li>
            </ul>
            <Carousel ref={slider} effect="scrollx" beforeChange={onChange}>
              <div className="slide">
                <p>
                  <Translate id="home.selfServe.text.dashboards">
                    Explore data and find insights from interactive dashboards.
                  </Translate>
                </p>
              </div>
              <div className="slide">
                <p>
                  <Translate id="home.selfServe.text.chartBuilder">
                    Drag and drop to create robust charts and tables.
                  </Translate>
                </p>
              </div>
              <div className="slide">
                <p>
                  <Translate id="home.selfServe.text.sqlLab">
                    Write custom SQL queries, browse database metadata, use
                    Jinja templating, and more.
                  </Translate>
                </p>
              </div>
              <div className="slide">
                <p>
                  <Translate id="home.selfServe.text.datasets">
                    Create physical and virtual datasets to scale chart creation
                    with unified metric definitions.
                  </Translate>
                </p>
              </div>
            </Carousel>
            <video autoPlay muted controls loop>
              <source
                src="https://superset.staged.apache.org/superset-video-4k.mp4"
                type="video/mp4"
              />
            </video>
          </StyledSliderSection>
          <StyledKeyFeatures>
            <h3>
              <Translate id="home.keyFeatures.title">Key features</Translate>
            </h3>
            <div className="grid">
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f1">
                    40+ pre-installed visualizations inherited from Apache
                    Superset
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f2">
                    Full async stack: Litestar + Uvicorn + uvloop +
                    SQLAlchemy 2.0
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f3">
                    Native async drivers for Postgres, MySQL, ClickHouse, Trino
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f4">
                    msgspec-powered serialization (replaces Marshmallow and
                    Pydantic v1)
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f5">
                    Drop-in compatibility: same metadata DB schema, same REST
                    API, same SPA frontend
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f6">
                    Native Litestar WebSocket — no separate Node.js
                    superset-websocket service
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f7">
                    Flask session cookie / CSRF token compatibility — sessions
                    survive the migration
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f8">
                    Auto-generated OpenAPI docs at /swagger/v1
                  </Translate>
                </div>
              </div>
              <div className="item">
                <img src="/img/check-icon.svg" alt="check-icon" />
                <div>
                  <Translate id="home.keyFeatures.f9">
                    structlog JSON logging out of the box
                  </Translate>
                </div>
              </div>
            </div>
          </StyledKeyFeatures>
        </BlurredSection>
        <BlurredSection>
          <StyledBenchmarksSection>
            <SectionHeader
              level="h2"
              title={translate({
                id: 'home.benchmarks.title',
                message: 'Performance & Benchmarks',
              })}
              subtitle={translate({
                id: 'home.benchmarks.subtitle',
                message:
                  'Liteset is being benchmarked against Apache Superset 6.0.0 on identical workloads. Below — a preview of the metrics we collect; full methodology and results live in the testing report.',
              })}
            />
            <BenchmarkCharts />
            <Link to="/docs/benchmarks/results" className="benchmark-cta">
              <Translate id="home.benchmarks.cta">
                Read the full testing report →
              </Translate>
            </Link>
          </StyledBenchmarksSection>
        </BlurredSection>
        <BlurredSection>
          <StyledIntegrations>
            <SectionHeader
              level="h2"
              title={translate({
                id: 'home.databases.title',
                message: 'Supported Databases',
              })}
            />
            <div className="database-grid">
              {Databases.map(({ title, href, imgName }) => (
                <div className="item" key={title}>
                  {href ? (
                    <a href={href} aria-label={`Go to ${title} page`}>
                      <img src={`/img/databases/${imgName}`} title={title} />
                    </a>
                  ) : (
                    <img src={`/img/databases/${imgName}`} title={title} />
                  )}
                </div>
              ))}
            </div>
            <span className="database-sub">
              <Translate id="home.databases.more">
                ...and many other
              </Translate>{' '}
              <a href="/docs/configuration/databases#installing-database-drivers">
                <Translate id="home.databases.compatibleLink">
                  compatible databases
                </Translate>
              </a>
            </span>
          </StyledIntegrations>
        </BlurredSection>
      </StyledMain>
    </Layout>
  );
}
