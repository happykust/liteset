"use strict";(self.webpackChunkliteset_docs=self.webpackChunkliteset_docs||[]).push([[4583],{7824:(e,t,i)=>{i.d(t,{A:()=>c});var a=i(51322),s=i(33126),r=i(74848);const o=(0,a.A)("div")`
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 75px 20px 0;
  max-width: 720px;
  margin: 0 auto;
  ${s.mq[1]} {
    padding-top: 55px;
  }
  .title,
  .subtitle {
    color: ${e=>e.dark?"var(--ifm-font-base-color-inverse)":"var(--ifm-font-base-color)"};
  }
`,n=(0,a.A)(o)`
  .title {
    font-size: 96px;
    ${s.mq[1]} {
      font-size: 46px;
    }
  }
  .line {
    margin-top: -45px;
    margin-bottom: 15px;
    ${s.mq[1]} {
      margin-top: -20px;
      margin-bottom: 30px;
    }
  }
  .subtitle {
    font-size: 30px;
    line-height: 40px;
    ${s.mq[1]} {
      font-size: 25px;
      line-height: 29px;
    }
  }
`,l=(0,a.A)(o)`
  .title {
    font-size: 48px;
    ${s.mq[1]} {
      font-size: 34px;
    }
  }
  .line {
    margin-top: -15px;
    margin-bottom: 15px;
    ${s.mq[1]} {
      margin-top: -5px;
    }
  }
  .subtitle {
    font-size: 24px;
    line-height: 32px;
    ${s.mq[1]} {
      font-size: 18px;
      line-height: 26px;
    }
  }
`,c=({level:e,title:t,subtitle:i,dark:a})=>{const s=e,o="h1"===e?n:l;return(0,r.jsxs)(o,{dark:!!a,children:[(0,r.jsx)(s,{className:"title",children:t}),(0,r.jsx)("img",{className:"line",src:"/img/community/line.png",alt:"line"}),i&&(0,r.jsx)("div",{className:"subtitle",children:i})]})}},33126:(e,t,i)=>{i.d(t,{mq:()=>a});const a=[576,768,992,1200].map((e=>`@media (max-width: ${e}px)`))},58916:(e,t,i)=>{i.d(t,{A:()=>d});var a=i(51322),s=i(33126),r=i(74848);const o=(0,a.A)("div")`
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
  max-width: 1160px;
  margin: 30px auto 0;
  padding: 0 20px;
  ${s.mq[1]} {
    grid-template-columns: 1fr;
  }
`,n=(0,a.A)("div")`
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
    ${s.mq[1]} {
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
`,l=(0,a.A)("p")`
  max-width: 800px;
  margin: 0 auto 20px;
  text-align: center;
  font-size: 14px;
  font-style: italic;
  color: var(--ifm-secondary-text);
`,c=[{id:"rps",title:"Throughput (RPS)",subtitle:"Dashboard Fan-Out, 200 concurrent users",unit:"req/s",betterDirection:"higher",bars:[{label:"Apache Superset",value:1.27},{label:"Liteset",value:10.57,highlight:!0}]},{id:"latency",title:"Median response time",subtitle:"Dashboard Fan-Out, 200 concurrent users",unit:"ms",betterDirection:"lower",bars:[{label:"Apache Superset",value:134e3},{label:"Liteset",value:4500,highlight:!0}]},{id:"errors",title:"Error rate",subtitle:"Dashboard Fan-Out, 200 concurrent users",unit:"%",betterDirection:"lower",bars:[{label:"Apache Superset",value:32.8},{label:"Liteset",value:7.4,highlight:!0}]},{id:"io-sweep",title:"Throughput at 1 s I/O latency",subtitle:"Controlled IO Latency Sweep, 50 users",unit:"req/s",betterDirection:"higher",bars:[{label:"Apache Superset",value:2.47},{label:"Liteset",value:25.52,highlight:!0}]}],d=({charts:e=c,showPlaceholderNotice:t=!1,placeholderNotice:i})=>{const a=i??"Measured on identical hardware against the SSB SF=10 dataset; see the methodology page.";return(0,r.jsxs)(r.Fragment,{children:[t&&(0,r.jsx)(l,{children:a}),(0,r.jsx)(o,{children:e.map((e=>{const t=Math.max(...e.bars.map((e=>e.value)));return(0,r.jsxs)(n,{children:[(0,r.jsxs)("div",{className:"card-header",children:[(0,r.jsx)("h3",{className:"title",children:e.title}),(0,r.jsx)("span",{className:"direction",children:"higher"===e.betterDirection?"\u2191 better":"\u2193 better"})]}),e.subtitle&&(0,r.jsx)("p",{className:"subtitle",children:e.subtitle}),e.bars.map((i=>{const a=t>0?i.value/t*100:0;return(0,r.jsxs)("div",{className:"bar-row",children:[(0,r.jsx)("span",{className:"bar-label",children:i.label}),(0,r.jsx)("span",{className:"bar-track",children:(0,r.jsx)("span",{className:"bar-fill"+(i.highlight?" highlight":""),style:{width:`${a}%`}})}),(0,r.jsxs)("span",{className:"bar-value",children:[i.value.toLocaleString()," ",i.unit??e.unit]})]},i.label)}))]},e.id)}))})]})}},65986:(e,t,i)=>{i.r(t),i.d(t,{default:()=>q});var a=i(96540),s=i(79139),r=i(28774),o=i(21312),n=i(44586),l=i(36591),c=i(51322),d=i(20072),m=i(33126);const p=[{title:"PostgreSQL",href:"https://www.postgresql.org/",imgName:"postgresql.svg"},{title:"BigQuery",href:"https://cloud.google.com/bigquery/",imgName:"google-big-query.svg"},{title:"Snowflake",href:"https://www.snowflake.com/",imgName:"snowflake.svg"},{title:"MySQL",href:"https://www.mysql.com/",imgName:"mysql.jpg"},{title:"Amazon Redshift",href:"https://aws.amazon.com/redshift/",imgName:"amazon-redshift.jpg"},{title:"Amazon Athena",href:"https://aws.amazon.com/pt/athena/",imgName:"amazon-athena.jpg"},{title:"Apache Druid",href:"https://druid.apache.org/",imgName:"druid.png"},{title:"Databricks",href:"https://www.databricks.com",imgName:"databricks.png"},{title:"Google Sheets",href:"https://www.google.com/sheets/about/",imgName:"google-sheets.svg"},{title:"CSV",imgName:"csv.svg"},{title:"ClickHouse",href:"https://clickhouse.com/",imgName:"clickhouse.png"},{title:"Dremio",href:"https://www.dremio.com/",imgName:"dremio.png"},{title:"Trino",href:"https://trino.io/",imgName:"trino2.jpg"},{title:"Oracle",href:"https://www.oracle.com/database/",imgName:"oraclelogo.png"},{title:"Apache Pinot",href:"https://pinot.apache.org/",imgName:"apache-pinot.svg"},{title:"Presto",href:"https://prestodb.io/",imgName:"presto-og.png"},{title:"IBM Db2",href:"https://www.ibm.com/products/db2",imgName:"ibmdb2.png"},{title:"SAP Hana",href:"https://www.sap.com/products/data-cloud/hana.html",imgName:"sap-hana.jpg"},{title:"Microsoft SqlServer",href:"https://www.microsoft.com/en-us/sql-server",imgName:"msql.png"},{title:"Apache Doris",href:"https://doris.apache.org/",imgName:"doris.png"},{title:"OceanBase",href:"https://www.oceanbase.com/",imgName:"oceanbase.svg"},{title:"Couchbase",href:"https://www.couchbase.com/",imgName:"couchbase.svg"},{title:"Denodo",href:"https://www.denodo.com/",imgName:"denodo.png"},{title:"TDengine",href:"https://tdengine.com/",imgName:"tdengine.png"}];var h=i(7824),g=i(77145),x=i(58916),u=i(74848);const b=[{image:"powerful-yet-easy.jpg",titleId:"home.features.powerful.title",titleDefault:"Powerful, but async",descriptionId:"home.features.powerful.description",descriptionDefault:"Liteset preserves the no-code chart builder and SQL Lab from Apache Superset, but the entire web layer runs on a single ASGI event loop instead of pre-forked Flask workers."},{image:"modern-databases.jpg",titleId:"home.features.databases.title",titleDefault:"Modern databases, native async drivers",descriptionId:"home.features.databases.description",descriptionDefault:"Postgres, MySQL, ClickHouse, and Trino use native async drivers (asyncpg, asyncmy, aiochclient, aiotrino). Other databases keep working through a sync-fallback wrapper."},{image:"modern-architecture.jpg",titleId:"home.features.architecture.title",titleDefault:"Clean async architecture",descriptionId:"home.features.architecture.description",descriptionDefault:"Four layers \u2014 Controllers, Commands, DAOs, AsyncSession \u2014 built on Litestar, SQLAlchemy 2.0 and msgspec. No Flask, no synchronous I/O on the hot path."},{image:"rich-visualizations.jpg",titleId:"home.features.compat.title",titleDefault:"Drop-in compatibility",descriptionId:"home.features.compat.description",descriptionDefault:"The metadata DB schema, REST API, WebSocket contract and SPA frontend are inherited 1:1 from Apache Superset 6.0.0. Stop Superset, start Liteset on the same database."}],f=(0,c.A)("main")`
  text-align: center;
`,v=(0,c.A)("div")`
  position: relative;
  padding: 130px 20px 0;
  margin-bottom: 160px;
  background-image: url('/img/grid-background.jpg');
  background-size: cover;
  ${m.mq[1]} {
    margin-bottom: 100px;
  }
  .info-container {
    position: relative;
    z-index: 4;
  }
  .superset-mark {
    ${m.mq[1]} {
      width: 140px;
    }
  }
  .info-text {
    font-size: 30px;
    line-height: 37px;
    max-width: 720px;
    margin: 24px auto 10px;
    color: var(--ifm-font-base-color-inverse);
    ${m.mq[1]} {
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
    ${m.mq[1]} {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .github-button {
      margin: 5px;
      ${m.mq[1]} {
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
`,j=(0,c.A)(r.A)`
  border-radius: 10px;
  font-size: 20px;
  font-weight: bold;
  width: 170px;
  padding: 10px 0;
  margin: 15px auto 0;
  ${m.mq[1]} {
    font-size: 19px;
    width: 175px;
    padding: 10px 0;
  }
`,w=(0,c.A)("div")`
  position: relative;
  display: inline-block;
  padding-top: 30px;
  margin-top: 25px;
  margin-bottom: -125px;
  max-width: 800px;
  ${m.mq[1]} {
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
    ${m.mq[1]} {
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
    ${m.mq[1]} {
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
    ${m.mq[1]} {
      display: block;
    }
  }
`,k=(0,c.A)("ul")`
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  width: 100%;
  max-width: 1170px;
  margin: 15px auto 0;
  padding: 0 20px;
  ${m.mq[1]} {
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
    ${m.mq[1]} {
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
      ${m.mq[1]} {
        width: 115px;
      }
    }
    .title {
      font-size: 24px;
      margin: 10px 0 0;
      ${m.mq[1]} {
        font-size: 23px;
        margin-top: 20px;
      }
    }
    .description {
      font-size: 17px;
      line-height: 23px;
      margin: 5px 0 0;
      ${m.mq[1]} {
        font-size: 16px;
        margin-top: 10px;
      }
    }
  }
`,y=(0,c.A)("div")`
  position: relative;
  padding: 60px 20px;
  ${m.mq[1]} {
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
    ${m.mq[1]} {
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
    ${m.mq[1]} {
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
      ${m.mq[1]} {
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
        ${m.mq[1]} {
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
      ${m.mq[1]} {
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
    ${m.mq[1]} {
      border-radius: 5px;
    }
  }
`,A=(0,c.A)("div")`
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
    ${m.mq[1]} {
      grid-template-columns: repeat(1, minmax(0, 1fr));
    }
    & > .item {
      display: flex;
      font-size: 17px;
      ${m.mq[1]} {
        font-size: 15px;
      }
      & > img {
        width: 20px;
        height: 20px;
        flex-shrink: 0;
        margin-right: 12px;
        margin-top: 4px;
        ${m.mq[1]} {
          width: 18px;
          height: 18px;
          margin-top: 2px;
        }
      }
    }
  }
`,N=(0,c.A)("div")`
  padding: 0 20px;
  .database-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 14px;
    max-width: 1160px;
    margin: 25px auto 0;
    ${m.mq[1]} {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    ${m.mq[0]} {
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
`,S=(0,c.A)("div")`
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
`;function q(){const e=(0,a.useRef)(null),{siteConfig:t}=(0,n.A)(),i=t.customFields?.litesetGithub??"https://github.com/happykust/liteset",c=t.customFields?.upstreamGithub??"https://github.com/apache/superset",m=t.customFields?.litesetVersion??"6.0.0",[q,z]=(0,a.useState)(0),$=()=>{const e=document.body.querySelector(".navbar"),t=document.body.querySelector(".navbar__logo img");e&&t&&(e.classList.add("navbar--dark"),t.setAttribute("src","/img/liteset-logo-horiz-dark.svg"))},L=()=>{const e=document.body.querySelector(".navbar"),t=document.body.querySelector(".navbar__logo img");e&&t&&(e.classList.remove("navbar--dark"),t.setAttribute("src","/img/liteset-logo-horiz.svg"))};return(0,a.useEffect)((()=>{$();const e=document.body.querySelector(".navbar__toggle"),t=()=>L();e&&e.addEventListener("click",t);const i=()=>{window.scrollY>0?L():$()};return window.addEventListener("scroll",i),()=>{window.removeEventListener("scroll",i),e&&e.removeEventListener("click",t),L()}}),[]),(0,u.jsx)(s.A,{title:(0,o.T)({id:"home.meta.title",message:"Welcome"}),description:(0,o.T)({id:"home.meta.description",message:"Liteset \u2014 async port of Apache Superset on Litestar/ASGI with full backward compatibility"}),wrapperClassName:"under-navbar",children:(0,u.jsxs)(f,{children:[(0,u.jsxs)(v,{children:[(0,u.jsxs)("div",{className:"info-container",children:[(0,u.jsx)("img",{className:"superset-mark",src:"/img/liteset-mark-dark.svg",alt:"Liteset mark"}),(0,u.jsx)("div",{className:"info-text",children:(0,u.jsx)(o.A,{id:"home.hero.tagline",children:"Liteset is an async port of Apache Superset built on Litestar/ASGI \u2014 same dashboards, datasets, users and roles, a completely new web layer."})}),(0,u.jsx)("div",{children:(0,u.jsx)("span",{className:"version-pill",children:(0,u.jsx)(o.A,{id:"home.hero.versionPill",values:{version:m},children:"Liteset {version} \xb7 based on Apache Superset 6.0.0"})})}),(0,u.jsx)("img",{src:"/img/community/line.png",alt:"line"}),(0,u.jsxs)("div",{className:"github-section",children:[(0,u.jsx)("span",{className:"github-button",children:(0,u.jsx)(d.A,{href:i,"data-size":"large","data-show-count":"true","aria-label":"Star happykust/liteset on GitHub",children:"Star"})}),(0,u.jsx)("span",{className:"github-button",children:(0,u.jsx)(d.A,{href:c,"data-size":"large","data-show-count":"true","aria-label":"Star apache/superset on GitHub (upstream)",children:"Upstream"})}),(0,u.jsx)("span",{className:"github-button",children:(0,u.jsx)(d.A,{href:`${i}/fork`,"data-size":"large","data-show-count":"true","aria-label":"Fork happykust/liteset on GitHub",children:"Fork"})})]}),(0,u.jsx)("img",{src:"/img/community/line.png",alt:"line"}),(0,u.jsx)(j,{className:"default-button-theme",href:"/docs/intro",children:(0,u.jsx)(o.A,{id:"home.hero.cta",children:"Get Started"})})]}),(0,u.jsxs)(w,{children:[(0,u.jsx)("img",{className:"screenshot",src:"/img/hero-screenshot.jpg",alt:"hero-screenshot"}),(0,u.jsx)("div",{className:"screenshot-shadow-1"}),(0,u.jsx)("div",{className:"screenshot-shadow-2"}),(0,u.jsx)("div",{className:"screenshotBlur"})]})]}),(0,u.jsxs)(g.A,{children:[(0,u.jsx)(h.A,{level:"h2",title:(0,o.T)({id:"home.overview.title",message:"Overview"}),subtitle:(0,o.T)({id:"home.overview.subtitle",message:"Liteset keeps every familiar feature of Apache Superset and replaces the synchronous Flask backend with a single-loop ASGI server. Higher concurrency and lower tail latency at a modest memory cost, identical UX."})}),(0,u.jsx)(k,{children:b.map((({image:e,titleId:t,titleDefault:i,descriptionId:a,descriptionDefault:s})=>(0,u.jsxs)("li",{className:"item",children:[(0,u.jsx)("div",{className:"image",children:(0,u.jsx)("img",{src:`/img/features/${e}`})}),(0,u.jsxs)("div",{className:"content",children:[(0,u.jsx)("h4",{className:"title",children:(0,u.jsx)(o.A,{id:t,children:i})}),(0,u.jsx)("p",{className:"description",children:(0,u.jsx)(o.A,{id:a,children:s})})]})]},t)))})]}),(0,u.jsxs)(g.A,{children:[(0,u.jsxs)(y,{children:[(0,u.jsx)(h.A,{level:"h2",title:(0,o.T)({id:"home.selfServe.title",message:"Self-serve analytics for anyone"}),dark:!0}),(0,u.jsxs)("ul",{className:"toggleBtns",children:[(0,u.jsx)("li",{className:`toggle ${0===q?"active":null}`,onClick:()=>e.current.goTo(0),role:"button",children:(0,u.jsx)(o.A,{id:"home.selfServe.tab.dashboards",children:"Dashboards"})}),(0,u.jsx)("li",{className:`toggle ${1===q?"active":null}`,onClick:()=>e.current.goTo(1),role:"button",children:(0,u.jsx)(o.A,{id:"home.selfServe.tab.chartBuilder",children:"Chart Builder"})}),(0,u.jsx)("li",{className:`toggle ${2===q?"active":null}`,onClick:()=>e.current.goTo(2),role:"button",children:(0,u.jsx)(o.A,{id:"home.selfServe.tab.sqlLab",children:"SQL Lab"})}),(0,u.jsx)("li",{className:`toggle ${3===q?"active":null}`,onClick:()=>e.current.goTo(3),role:"button",children:(0,u.jsx)(o.A,{id:"home.selfServe.tab.datasets",children:"Datasets"})})]}),(0,u.jsxs)(l.A,{ref:e,effect:"scrollx",beforeChange:(e,t)=>{z(t)},children:[(0,u.jsx)("div",{className:"slide",children:(0,u.jsx)("p",{children:(0,u.jsx)(o.A,{id:"home.selfServe.text.dashboards",children:"Explore data and find insights from interactive dashboards."})})}),(0,u.jsx)("div",{className:"slide",children:(0,u.jsx)("p",{children:(0,u.jsx)(o.A,{id:"home.selfServe.text.chartBuilder",children:"Drag and drop to create robust charts and tables."})})}),(0,u.jsx)("div",{className:"slide",children:(0,u.jsx)("p",{children:(0,u.jsx)(o.A,{id:"home.selfServe.text.sqlLab",children:"Write custom SQL queries, browse database metadata, use Jinja templating, and more."})})}),(0,u.jsx)("div",{className:"slide",children:(0,u.jsx)("p",{children:(0,u.jsx)(o.A,{id:"home.selfServe.text.datasets",children:"Create physical and virtual datasets to scale chart creation with unified metric definitions."})})})]}),(0,u.jsx)("video",{autoPlay:!0,muted:!0,controls:!0,loop:!0,children:(0,u.jsx)("source",{src:"https://superset.staged.apache.org/superset-video-4k.mp4",type:"video/mp4"})})]}),(0,u.jsxs)(A,{children:[(0,u.jsx)("h3",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.title",children:"Key features"})}),(0,u.jsxs)("div",{className:"grid",children:[(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f1",children:"40+ pre-installed visualizations inherited from Apache Superset"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f2",children:"Full async stack: Litestar + Uvicorn + uvloop + SQLAlchemy 2.0"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f3",children:"Native async drivers for Postgres, MySQL, ClickHouse, Trino"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f4",children:"msgspec-powered serialization (replaces Marshmallow and Pydantic v1)"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f5",children:"Drop-in compatibility: same metadata DB schema, same REST API, same SPA frontend"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f6",children:"Native Litestar WebSocket \u2014 no separate Node.js superset-websocket service"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f7",children:"Flask session cookie / CSRF token compatibility \u2014 sessions survive the migration"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f8",children:"Auto-generated OpenAPI docs at /swagger/v1"})})]}),(0,u.jsxs)("div",{className:"item",children:[(0,u.jsx)("img",{src:"/img/check-icon.svg",alt:"check-icon"}),(0,u.jsx)("div",{children:(0,u.jsx)(o.A,{id:"home.keyFeatures.f9",children:"structlog JSON logging out of the box"})})]})]})]})]}),(0,u.jsx)(g.A,{children:(0,u.jsxs)(S,{children:[(0,u.jsx)(h.A,{level:"h2",title:(0,o.T)({id:"home.benchmarks.title",message:"Performance & Benchmarks"}),subtitle:(0,o.T)({id:"home.benchmarks.subtitle",message:"Liteset was benchmarked against Apache Superset 6.0.0 on identical hardware and workloads (SSB SF=10 dataset, Locust load generator). Below \u2014 the headline metrics; full methodology and results live in the testing report."})}),(0,u.jsx)(x.A,{}),(0,u.jsx)(r.A,{to:"/docs/benchmarks/results",className:"benchmark-cta",children:(0,u.jsx)(o.A,{id:"home.benchmarks.cta",children:"Read the full testing report \u2192"})})]})}),(0,u.jsx)(g.A,{children:(0,u.jsxs)(N,{children:[(0,u.jsx)(h.A,{level:"h2",title:(0,o.T)({id:"home.databases.title",message:"Supported Databases"})}),(0,u.jsx)("div",{className:"database-grid",children:p.map((({title:e,href:t,imgName:i})=>(0,u.jsx)("div",{className:"item",children:t?(0,u.jsx)("a",{href:t,"aria-label":`Go to ${e} page`,children:(0,u.jsx)("img",{src:`/img/databases/${i}`,title:e})}):(0,u.jsx)("img",{src:`/img/databases/${i}`,title:e})},e)))}),(0,u.jsxs)("span",{className:"database-sub",children:[(0,u.jsx)(o.A,{id:"home.databases.more",children:"...and many other"})," ",(0,u.jsx)("a",{href:"/docs/configuration/databases#installing-database-drivers",children:(0,u.jsx)(o.A,{id:"home.databases.compatibleLink",children:"compatible databases"})})]})]})})]})})}},77145:(e,t,i)=>{i.d(t,{A:()=>n});var a=i(51322),s=i(33126),r=i(74848);const o=(0,a.A)("section")`
  text-align: center;
  border-bottom: 1px solid var(--ifm-border-color);
  overflow: hidden;
  .blur {
    max-width: 635px;
    width: 100%;
    margin-top: -70px;
    margin-bottom: -35px;
    position: relative;
    z-index: -1;
    ${s.mq[1]} {
      margin-top: -40px;
    }
  }
`,n=({children:e})=>(0,r.jsxs)(o,{children:[e,(0,r.jsx)("img",{className:"blur",src:"/img/community/blur.png",alt:"Blur"})]})}}]);