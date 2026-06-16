"use strict";(self.webpackChunkliteset_docs=self.webpackChunkliteset_docs||[]).push([[8880],{7824:(e,t,i)=>{i.d(t,{A:()=>m});var s=i(51322),r=i(33126),o=i(74848);const n=(0,s.A)("div")`
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 75px 20px 0;
  max-width: 720px;
  margin: 0 auto;
  ${r.mq[1]} {
    padding-top: 55px;
  }
  .title,
  .subtitle {
    color: ${e=>e.dark?"var(--ifm-font-base-color-inverse)":"var(--ifm-font-base-color)"};
  }
`,a=(0,s.A)(n)`
  .title {
    font-size: 96px;
    ${r.mq[1]} {
      font-size: 46px;
    }
  }
  .line {
    margin-top: -45px;
    margin-bottom: 15px;
    ${r.mq[1]} {
      margin-top: -20px;
      margin-bottom: 30px;
    }
  }
  .subtitle {
    font-size: 30px;
    line-height: 40px;
    ${r.mq[1]} {
      font-size: 25px;
      line-height: 29px;
    }
  }
`,l=(0,s.A)(n)`
  .title {
    font-size: 48px;
    ${r.mq[1]} {
      font-size: 34px;
    }
  }
  .line {
    margin-top: -15px;
    margin-bottom: 15px;
    ${r.mq[1]} {
      margin-top: -5px;
    }
  }
  .subtitle {
    font-size: 24px;
    line-height: 32px;
    ${r.mq[1]} {
      font-size: 18px;
      line-height: 26px;
    }
  }
`,m=({level:e,title:t,subtitle:i,dark:s})=>{const r=e,n="h1"===e?a:l;return(0,o.jsxs)(n,{dark:!!s,children:[(0,o.jsx)(r,{className:"title",children:t}),(0,o.jsx)("img",{className:"line",src:"/img/community/line.png",alt:"line"}),i&&(0,o.jsx)("div",{className:"subtitle",children:i})]})}},22985:(e,t,i)=>{i.r(t),i.d(t,{default:()=>x});var s=i(51322),r=i(17502),o=i(79139),n=i(21312),a=i(44586),l=i(33126),m=i(7824),p=i(77145),c=i(74848);const u=(0,s.A)("section")`
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
    ${l.mq[1]} {
      width: 40px;
      margin-top: 0;
    }
  }
  .title {
    font-size: 20px;
    line-height: 36px;
    font-weight: 700;
    color: var(--ifm-font-base-color);
    ${l.mq[1]} {
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
    ${l.mq[1]} {
      font-size: 17px;
      line-height: 22px;
      color: var(--ifm-primary-text);
      margin-bottom: 35px;
      margin-top: 0;
    }
  }
`,d=(0,s.A)("section")`
  max-width: 720px;
  margin: 30px auto 60px;
  padding: 0 20px;
  text-align: center;
  font-size: 16px;
  line-height: 24px;
  color: var(--ifm-secondary-text);
`,x=()=>{const{siteConfig:e}=(0,a.A)(),t=e.customFields?.litesetGithub??"https://github.com/happykust/liteset",i=[{url:t,titleId:"community.link.github.title",titleDefault:"GitHub repository",descriptionId:"community.link.github.description",descriptionDefault:"Source code, releases, contributing guidelines and the diploma testing report.",image:"github-symbol.jpg",ariaLabel:"Open the Liteset GitHub repository"},{url:`${t}/issues`,titleId:"community.link.issues.title",titleDefault:"Issues & feature requests",descriptionId:"community.link.issues.description",descriptionDefault:'Report regressions versus Apache Superset 6.0.0, request features, or pick up a "good first issue".',image:"note-symbol.png",ariaLabel:"Open Liteset issues on GitHub"},{url:`${t}/discussions`,titleId:"community.link.discussions.title",titleDefault:"Discussions",descriptionId:"community.link.discussions.description",descriptionDefault:"Ask questions, share migration experience and benchmark results.",image:"coffee-symbol.png",ariaLabel:"Open Liteset discussions on GitHub"},{url:e.customFields?.upstreamGithub??"https://github.com/apache/superset",titleId:"community.link.upstream.title",titleDefault:"Upstream \u2014 Apache Superset",descriptionId:"community.link.upstream.description",descriptionDefault:"Liteset tracks Apache Superset 6.0.0 \u2014 most product questions and bug reports belong upstream.",image:"writing-symbol.png",ariaLabel:"Open the Apache Superset GitHub repository"}];return(0,c.jsx)(o.A,{title:(0,n.T)({id:"community.meta.title",message:"Community"}),description:(0,n.T)({id:"community.meta.description",message:"Liteset community \u2014 async port of Apache Superset 6.0.0. GitHub, issues, discussions and the upstream project."}),children:(0,c.jsxs)("main",{children:[(0,c.jsx)(p.A,{children:(0,c.jsx)(m.A,{level:"h1",title:(0,n.T)({id:"community.header.title",message:"Community"}),subtitle:(0,n.T)({id:"community.header.subtitle",message:"Liteset is an academic port. The community is small but the upstream Apache Superset community is large and welcoming."})})}),(0,c.jsx)(u,{children:(0,c.jsx)(r.A,{className:"list",itemLayout:"horizontal",dataSource:i,renderItem:({url:e,titleId:t,titleDefault:i,descriptionId:s,descriptionDefault:o,image:a,ariaLabel:l})=>(0,c.jsx)(r.A.Item,{className:"item",children:(0,c.jsx)(r.A.Item.Meta,{avatar:(0,c.jsx)("a",{className:"title",href:e,target:"_blank",rel:"noreferrer","aria-label":l,children:(0,c.jsx)("img",{className:"icon",src:`/img/community/${a}`})}),title:(0,c.jsx)("a",{href:e,target:"_blank",rel:"noreferrer",children:(0,c.jsx)("p",{className:"title",style:{marginBottom:0},children:(0,c.jsx)(n.A,{id:t,children:i})})}),description:(0,c.jsx)("p",{className:"description",children:(0,c.jsx)(n.A,{id:s,children:o})}),"aria-label":"Community link"})})})}),(0,c.jsx)(d,{children:(0,c.jsx)(n.A,{id:"community.upstreamNote",children:"Looking for the broader Superset community \u2014 Slack, dev mailing list, monthly meetups, Stack Overflow tag? They live in the upstream Apache Superset project. Liteset reuses the same product; most product-level questions are best asked there."})})]})})}},33126:(e,t,i)=>{i.d(t,{mq:()=>s});const s=[576,768,992,1200].map((e=>`@media (max-width: ${e}px)`))},77145:(e,t,i)=>{i.d(t,{A:()=>a});var s=i(51322),r=i(33126),o=i(74848);const n=(0,s.A)("section")`
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
    ${r.mq[1]} {
      margin-top: -40px;
    }
  }
`,a=({children:e})=>(0,o.jsxs)(n,{children:[e,(0,o.jsx)("img",{className:"blur",src:"/img/community/blur.png",alt:"Blur"})]})}}]);