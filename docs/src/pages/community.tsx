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
import styled from '@emotion/styled';
import { List } from 'antd';
import Layout from '@theme/Layout';
import Translate, { translate } from '@docusaurus/Translate';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import { mq } from '../utils';
import SectionHeader from '../components/SectionHeader';
import BlurredSection from '../components/BlurredSection';

interface CommunityLink {
  url: string;
  titleId: string;
  titleDefault: string;
  descriptionId: string;
  descriptionDefault: string;
  image: string;
  ariaLabel: string;
}

const StyledJoinCommunity = styled('section')`
  background-color: var(--ifm-background-color);
  border-bottom: 1px solid var(--ifm-border-color);
  .list {
    max-width: 540px;
    margin: 0 auto;
    padding: 40px 20px 20px 35px;
  }
  .item {
    padding: 0;
    border: 0;
  }
  .icon {
    width: 40px;
    margin-top: 5px;
    ${mq[1]} {
      width: 40px;
      margin-top: 0;
    }
  }
  .title {
    font-size: 20px;
    line-height: 36px;
    font-weight: 700;
    color: var(--ifm-font-base-color);
    ${mq[1]} {
      font-size: 23px;
      line-height: 26px;
    }
  }
  .description {
    font-size: 14px;
    line-height: 20px;
    color: var(--ifm-font-base-color);
    margin-top: -8px;
    margin-bottom: 23px;
    ${mq[1]} {
      font-size: 17px;
      line-height: 22px;
      color: var(--ifm-primary-text);
      margin-bottom: 35px;
      margin-top: 0;
    }
  }
`;

const StyledUpstreamNote = styled('section')`
  max-width: 720px;
  margin: 30px auto 60px;
  padding: 0 20px;
  text-align: center;
  font-size: 16px;
  line-height: 24px;
  color: var(--ifm-secondary-text);
`;

const Community = () => {
  const { siteConfig } = useDocusaurusContext();
  const litesetGithub =
    (siteConfig.customFields?.litesetGithub as string) ??
    'https://github.com/happykust/liteset';
  const upstreamGithub =
    (siteConfig.customFields?.upstreamGithub as string) ??
    'https://github.com/apache/superset';

  const communityLinks: CommunityLink[] = [
    {
      url: litesetGithub,
      titleId: 'community.link.github.title',
      titleDefault: 'GitHub repository',
      descriptionId: 'community.link.github.description',
      descriptionDefault:
        'Source code, releases, contributing guidelines and the diploma testing report.',
      image: 'github-symbol.jpg',
      ariaLabel: 'Open the Liteset GitHub repository',
    },
    {
      url: `${litesetGithub}/issues`,
      titleId: 'community.link.issues.title',
      titleDefault: 'Issues & feature requests',
      descriptionId: 'community.link.issues.description',
      descriptionDefault:
        'Report regressions versus Apache Superset 6.0.0, request features, or pick up a "good first issue".',
      image: 'note-symbol.png',
      ariaLabel: 'Open Liteset issues on GitHub',
    },
    {
      url: `${litesetGithub}/discussions`,
      titleId: 'community.link.discussions.title',
      titleDefault: 'Discussions',
      descriptionId: 'community.link.discussions.description',
      descriptionDefault:
        'Ask questions, share migration experience and benchmark results.',
      image: 'coffee-symbol.png',
      ariaLabel: 'Open Liteset discussions on GitHub',
    },
    {
      url: upstreamGithub,
      titleId: 'community.link.upstream.title',
      titleDefault: 'Upstream — Apache Superset',
      descriptionId: 'community.link.upstream.description',
      descriptionDefault:
        'Liteset tracks Apache Superset 6.0.0 — most product questions and bug reports belong upstream.',
      image: 'writing-symbol.png',
      ariaLabel: 'Open the Apache Superset GitHub repository',
    },
  ];

  return (
    <Layout
      title={translate({
        id: 'community.meta.title',
        message: 'Community',
      })}
      description={translate({
        id: 'community.meta.description',
        message:
          'Liteset community — async port of Apache Superset 6.0.0. GitHub, issues, discussions and the upstream project.',
      })}
    >
      <main>
        <BlurredSection>
          <SectionHeader
            level="h1"
            title={translate({
              id: 'community.header.title',
              message: 'Community',
            })}
            subtitle={translate({
              id: 'community.header.subtitle',
              message:
                'Liteset is an academic port. The community is small but the upstream Apache Superset community is large and welcoming.',
            })}
          />
        </BlurredSection>
        <StyledJoinCommunity>
          <List
            className="list"
            itemLayout="horizontal"
            dataSource={communityLinks}
            renderItem={({
              url,
              titleId,
              titleDefault,
              descriptionId,
              descriptionDefault,
              image,
              ariaLabel,
            }) => (
              <List.Item className="item">
                <List.Item.Meta
                  avatar={
                    <a
                      className="title"
                      href={url}
                      target="_blank"
                      rel="noreferrer"
                      aria-label={ariaLabel}
                    >
                      <img className="icon" src={`/img/community/${image}`} />
                    </a>
                  }
                  title={
                    <a href={url} target="_blank" rel="noreferrer">
                      <p className="title" style={{ marginBottom: 0 }}>
                        <Translate id={titleId}>{titleDefault}</Translate>
                      </p>
                    </a>
                  }
                  description={
                    <p className="description">
                      <Translate id={descriptionId}>
                        {descriptionDefault}
                      </Translate>
                    </p>
                  }
                  aria-label="Community link"
                />
              </List.Item>
            )}
          />
        </StyledJoinCommunity>
        <StyledUpstreamNote>
          <Translate id="community.upstreamNote">
            Looking for the broader Superset community — Slack, dev mailing
            list, monthly meetups, Stack Overflow tag? They live in the
            upstream Apache Superset project. Liteset reuses the same product;
            most product-level questions are best asked there.
          </Translate>
        </StyledUpstreamNote>
      </main>
    </Layout>
  );
};

export default Community;
