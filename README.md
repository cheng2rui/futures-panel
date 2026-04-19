# futures-panel

期货风控面板 — 实时持仓管理 + 双向评分 + 回测系统

## 功能

- **实时行情** — 持仓/自选全品种行情实时监控（SSE推送）
- **双向评分** — 基于支撑/阻力/KDJ/波动率/OI/布林带的综合风险评分
- **R/R 风险收益** — 多空双向星级显示
- **趋势突破回测** — 支撑阻力突破策略，支持移动止损
- **均值回归回测** — 支撑阻力回归策略，更耐心的止盈逻辑
- **K线图表** — 回测结果可视化（Canvas绘制，支持入场/出场标记）
- **VaR 风险计算** — 日收益率标准差 × z值
- **保证金预警** — 杠杆倍数实时监控

## 快速开始

### Docker（推荐）

```bash
docker build -t futures-panel .
docker run -p 8318:8318 futures-panel
```

或使用 docker-compose：

```bash
docker-compose up -d
```

### 本地运行

```bash
pip install -r requirements.txt
python app.py --port 8318
```

访问 http://localhost:8318

## 技术栈

- **后端**：Python Flask + akshare
- **前端**：原生 HTML/CSS/JS（GridStack 布局）
- **数据源**：新浪财经实时行情

## 支持品种

覆盖国内全部期货品种：SHFE / DCE / CZCE / CFFEX / INE / GFEX

中金所品种（IM/IF/IH/IC 等）通过 `nf_` 前缀新浪接口获取。

## 版本

当前版本：v0.2.3
