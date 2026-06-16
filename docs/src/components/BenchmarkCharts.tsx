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
import { JSX } from 'react';
import styled from '@emotion/styled';
import { mq } from '../utils';

export interface BenchmarkBar {
  label: string;
  value: number;
  highlight?: boolean;
  unit?: string;
}

export interface BenchmarkChart {
  id: string;
  title: string;
  subtitle?: string;
  unit: string;
  betterDirection: 'higher' | 'lower';
  bars: BenchmarkBar[];
}

const StyledRoot = styled('div')`
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
  max-width: 1160px;
  margin: 30px auto 0;
  padding: 0 20px;
  ${mq[1]} {
    grid-template-columns: 1fr;
  }
`;

const StyledCard = styled('div')`
  border: 1px solid var(--ifm-border-color);
  border-radius: 12px;
  padding: 24px 24px 18px;
  background-color: var(--ifm-background-surface-color, var(--ifm-background-color));
  text-align: left;

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 4px;
  }
  .title {
    font-size: 20px;
    font-weight: 700;
    color: var(--ifm-font-base-color);
    margin: 0;
  }
  .direction {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--ifm-color-primary);
    flex-shrink: 0;
  }
  .subtitle {
    font-size: 14px;
    line-height: 18px;
    color: var(--ifm-secondary-text);
    margin: 0 0 18px;
  }
  .bar-row {
    display: grid;
    grid-template-columns: 110px 1fr 90px;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
    ${mq[1]} {
      grid-template-columns: 90px 1fr 80px;
    }
  }
  .bar-label {
    font-size: 14px;
    color: var(--ifm-font-base-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .bar-track {
    position: relative;
    height: 18px;
    border-radius: 6px;
    background-color: rgba(127, 127, 127, 0.15);
    overflow: hidden;
  }
  .bar-fill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    border-radius: 6px;
    background: linear-gradient(90deg, #51a6c5 0%, #20a7c9 100%);
    transition: width 0.6s ease;
  }
  .bar-fill.highlight {
    background: linear-gradient(90deg, #1fa8a8 0%, #0a8a8a 100%);
    box-shadow: 0 0 0 1px rgba(31, 168, 168, 0.3);
  }
  .bar-value {
    font-size: 14px;
    font-weight: 600;
    text-align: right;
    color: var(--ifm-font-base-color);
    font-variant-numeric: tabular-nums;
  }
`;

const StyledPlaceholderNotice = styled('p')`
  max-width: 800px;
  margin: 0 auto 20px;
  text-align: center;
  font-size: 14px;
  font-style: italic;
  color: var(--ifm-secondary-text);
`;

// Real measurements from the diploma load-testing report (Apache Superset 6.0.0
// on Flask + Gunicorn 4 sync workers vs Liteset on Litestar + Uvicorn 4 workers,
// identical hardware, SSB Scale Factor 10 dataset, Locust load generator).
const DEFAULT_CHARTS: BenchmarkChart[] = [
  {
    id: 'rps',
    title: 'Throughput (RPS)',
    subtitle: 'Dashboard Fan-Out, 200 concurrent users',
    unit: 'req/s',
    betterDirection: 'higher',
    bars: [
      { label: 'Apache Superset', value: 1.27 },
      { label: 'Liteset', value: 10.57, highlight: true },
    ],
  },
  {
    id: 'latency',
    title: 'Median response time',
    subtitle: 'Dashboard Fan-Out, 200 concurrent users',
    unit: 'ms',
    betterDirection: 'lower',
    bars: [
      { label: 'Apache Superset', value: 134000 },
      { label: 'Liteset', value: 4500, highlight: true },
    ],
  },
  {
    id: 'errors',
    title: 'Error rate',
    subtitle: 'Dashboard Fan-Out, 200 concurrent users',
    unit: '%',
    betterDirection: 'lower',
    bars: [
      { label: 'Apache Superset', value: 32.8 },
      { label: 'Liteset', value: 7.4, highlight: true },
    ],
  },
  {
    id: 'io-sweep',
    title: 'Throughput at 1 s I/O latency',
    subtitle: 'Controlled IO Latency Sweep, 50 users',
    unit: 'req/s',
    betterDirection: 'higher',
    bars: [
      { label: 'Apache Superset', value: 2.47 },
      { label: 'Liteset', value: 25.52, highlight: true },
    ],
  },
];

interface BenchmarkChartsProps {
  charts?: BenchmarkChart[];
  showPlaceholderNotice?: boolean;
  placeholderNotice?: string;
}

const BenchmarkCharts = ({
  charts = DEFAULT_CHARTS,
  showPlaceholderNotice = false,
  placeholderNotice,
}: BenchmarkChartsProps): JSX.Element => {
  const notice =
    placeholderNotice ??
    'Measured on identical hardware against the SSB SF=10 dataset; see the methodology page.';
  return (
    <>
      {showPlaceholderNotice && (
        <StyledPlaceholderNotice>{notice}</StyledPlaceholderNotice>
      )}
      <StyledRoot>
        {charts.map(chart => {
          const max = Math.max(...chart.bars.map(b => b.value));
          return (
            <StyledCard key={chart.id}>
              <div className="card-header">
                <h3 className="title">{chart.title}</h3>
                <span className="direction">
                  {chart.betterDirection === 'higher'
                    ? '↑ better'
                    : '↓ better'}
                </span>
              </div>
              {chart.subtitle && <p className="subtitle">{chart.subtitle}</p>}
              {chart.bars.map(bar => {
                const percent = max > 0 ? (bar.value / max) * 100 : 0;
                return (
                  <div className="bar-row" key={bar.label}>
                    <span className="bar-label">{bar.label}</span>
                    <span className="bar-track">
                      <span
                        className={`bar-fill${bar.highlight ? ' highlight' : ''}`}
                        style={{ width: `${percent}%` }}
                      />
                    </span>
                    <span className="bar-value">
                      {bar.value.toLocaleString()} {bar.unit ?? chart.unit}
                    </span>
                  </div>
                );
              })}
            </StyledCard>
          );
        })}
      </StyledRoot>
    </>
  );
};

export default BenchmarkCharts;
