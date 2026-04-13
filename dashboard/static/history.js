// history.js - Trade History & Performance Dashboard

class HistoryApp {
    constructor() {
        this.data = null;
        this.currentPage = 1;
        this.pageSize = 20;
        this.currentSort = { column: 'close_time', direction: 'desc' };
        this.chart = null;
        this.modalOpen = false;
        this.expandedTicket = null;

        // Static backtest reference (v3.1 18-month Aug 2024 - Feb 2026)
        this.backtestRef = {
            trades: 654,
            winRate: 72.3,
            profitFactor: 2.25,
            maxDrawdown: 196.83
        };

        // Poll every 60s
        this.pollingInterval = 60000;
        this.pollTimer = null;
    }

    async init() {
        await this.fetchData();
        this.startPolling();
        this.bindModalHandlers();
    }

    bindModalHandlers() {
        var modal = document.getElementById('trade-report-modal');
        if (!modal) return;
        var closeBtn = document.getElementById('trade-report-close');
        var self = this;
        if (closeBtn) closeBtn.addEventListener('click', function() { self.closeTradeReport(); });
        modal.addEventListener('click', function(e) {
            if (e.target === modal) self.closeTradeReport();
        });
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && self.modalOpen) self.closeTradeReport();
        });
    }

    async fetchData() {
        try {
            document.getElementById('loading-indicator').classList.remove('hidden');
            var response = await fetch('/api/history-data');
            if (!response.ok) throw new Error('Network response was not ok');
            var result = await response.json();
            if (result.error) throw new Error(result.error);

            this.data = result;

            if (this.currentSort.column !== 'close_time' || this.currentSort.direction !== 'desc') {
                this.sortData();
            } else {
                this.data.trades.sort(function(a, b) { return new Date(b.close_time) - new Date(a.close_time); });
            }

            this.render();
            this.fetchReadiness();
            document.getElementById('last-updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
            document.getElementById('loading-indicator').classList.add('hidden');
        } catch (error) {
            console.error('Error fetching history data:', error);
            document.getElementById('last-updated').textContent = 'Error: ' + error.message;
            document.getElementById('loading-indicator').classList.add('hidden');
        }
    }

    // FLO-272: Live Readiness Panel
    async fetchReadiness() {
        var metricsEl = document.getElementById('rp-metrics');
        try {
            var r = await fetch('/api/live-readiness');
            if (!r.ok) {
                console.error('Readiness fetch: HTTP ' + r.status);
                if (metricsEl) metricsEl.innerHTML = '<div style="color:#f87171;font-size:11px;padding:12px">Endpoint returned ' + r.status + '. Restart the dashboard server.</div>';
                return;
            }
            var data = await r.json();
            if (data.error) {
                console.error('Readiness API error:', data.error);
                if (metricsEl) metricsEl.innerHTML = '<div style="color:#f87171;font-size:11px;padding:12px">API error: ' + data.error + '</div>';
                return;
            }
            this.renderReadiness(data);
        } catch (e) {
            console.error('Readiness fetch error:', e);
            if (metricsEl) metricsEl.innerHTML = '<div style="color:#f87171;font-size:11px;padding:12px">Fetch failed: ' + e.message + '</div>';
        }
    }

    renderReadiness(data) {
        var criteria = data.criteria_met || 0;
        var total = data.criteria_total || 6;
        var status = data.status || 'NOT_READY';

        // Header status
        var statusEl = document.getElementById('rp-status');
        if (statusEl) {
            statusEl.className = 'rp-status rp-status-' + status;
            statusEl.textContent = status.replace(/_/g, ' ');
        }
        var countEl = document.getElementById('rp-criteria-count');
        if (countEl) {
            countEl.textContent = criteria + '/' + total;
            countEl.className = 'rp-criteria-count' + (criteria === total ? ' green' : criteria >= 3 ? ' yellow' : '');
        }

        // Overall progress
        var pct = (criteria / total) * 100;
        var fillEl = document.getElementById('rp-progress-fill');
        if (fillEl) {
            fillEl.style.width = pct + '%';
            fillEl.style.background = criteria === total
                ? 'linear-gradient(90deg, #4ade80, #22c55e)'
                : criteria >= 3
                    ? 'linear-gradient(90deg, #fbbf24, #f59e0b)'
                    : 'linear-gradient(90deg, #f87171, #ef4444)';
        }

        // Metric rows
        var metricOrder = [
            { key: 'profit_factor',   label: 'Profit Factor',    fmt: function(v){ return Number(v).toFixed(2); }, suffix: '' },
            { key: 'win_rate',        label: 'Win Rate',         fmt: function(v){ return Number(v).toFixed(1); }, suffix: '%' },
            { key: 'avg_win_loss',    label: 'Avg Win / Avg Loss', fmt: function(v){ return Number(v).toFixed(1); }, suffix: 'x' },
            { key: 'trades',          label: 'Trades',           fmt: function(v){ return String(v); }, suffix: '' },
            { key: 'max_drawdown',    label: 'Max Drawdown',     fmt: function(v){ return Number(v).toFixed(1); }, suffix: '%' },
            { key: 'days_without_p0', label: 'Days Without P0',  fmt: function(v){ return String(v); }, suffix: 'd' },
        ];

        var container = document.getElementById('rp-metrics');
        if (!container) return;
        var html = '';
        metricOrder.forEach(function(cfg) {
            var m = data.metrics[cfg.key];
            if (!m) return;
            var value = m.value;
            var level = m.level || 'below';
            var trend = m.trend || 'stable';
            var higherBetter = m.higher_is_better !== false;

            var trendSym = trend === 'improving' ? '▲' : trend === 'declining' ? '▼' : '─';
            var valClass = 'rp-value-' + level;
            var trendClass = 'rp-trend-' + trend;

            // Progress bar calculation
            // For higher-is-better: fill up to value/ideal*100, capped at 100
            // For lower-is-better (drawdown): fill is inverted — less is better
            var barPct, minPct, idealPct;
            var maxScale = higherBetter ? m.ideal * 1.1 : (m.min * 1.2);  // show up to slightly past ideal or past min
            if (higherBetter) {
                maxScale = Math.max(m.ideal * 1.1, value * 1.1, m.ideal + 1);
                barPct = Math.min(100, (value / maxScale) * 100);
                minPct = (m.min / maxScale) * 100;
                idealPct = (m.ideal / maxScale) * 100;
            } else {
                // drawdown-style: scale 0 -> min * 1.5
                maxScale = Math.max(m.min * 1.5, value * 1.2);
                barPct = Math.min(100, (value / maxScale) * 100);
                minPct = (m.min / maxScale) * 100;
                idealPct = (m.ideal / maxScale) * 100;
            }

            var fillClass = level;
            if (!higherBetter && level === 'ideal') fillClass = 'drawdown-ideal';

            var minLabelText = higherBetter ? 'min ' + cfg.fmt(m.min) + cfg.suffix : 'min <' + cfg.fmt(m.min) + cfg.suffix;
            var idealLabelText = 'ideal ' + cfg.fmt(m.ideal) + cfg.suffix;
            if (!higherBetter) idealLabelText = 'ideal ' + cfg.fmt(m.ideal) + cfg.suffix;

            html += '<div class="rp-metric">';
            html +=   '<div class="rp-metric-header">';
            html +=     '<span class="rp-metric-label">' + cfg.label + '</span>';
            html +=     '<span class="rp-metric-value ' + valClass + '">' + cfg.fmt(value) + cfg.suffix;
            html +=       '<span class="rp-trend ' + trendClass + '">' + trendSym + '</span>';
            html +=     '</span>';
            html +=   '</div>';
            html +=   '<div class="rp-metric-bar-wrap">';
            html +=     '<div class="rp-metric-bar-fill ' + fillClass + '" style="width:' + barPct + '%"></div>';
            html +=     '<div class="rp-marker rp-marker-min" style="left:' + minPct + '%"></div>';
            html +=     '<div class="rp-marker rp-marker-ideal" style="left:' + idealPct + '%"></div>';
            html +=   '</div>';
            html +=   '<div class="rp-metric-footer">';
            html +=     '<span class="zero-label">0</span>';
            html +=     '<span class="min-label" style="left:' + minPct + '%">' + minLabelText + '</span>';
            html +=     '<span class="ideal-label">' + idealLabelText + '</span>';
            html +=   '</div>';
            html += '</div>';
        });
        container.innerHTML = html;
    }

    startPolling() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        var self = this;
        this.pollTimer = setInterval(function() { self.fetchData(); }, this.pollingInterval);
    }

    render() {
        if (!this.data) return;
        this.renderGlobalStats();
        this.renderChart();
        this.renderComparisonBars();
        this.renderMonthlyTable();
        this.renderTradesTable();
    }

    // ── Stat Cards ──
    renderGlobalStats() {
        var stats = this.data.global_stats;
        var grid = document.getElementById('global-stats-grid');

        var beCount = stats.be_activation_count || 0;
        var beRate = stats.be_activation_rate || 0;
        var beDisplay = beCount > 0 ? beCount + ' (' + beRate + '%)' : '\u2014';

        var cards = [
            { label: 'Total Trades', value: stats.total_trades, accent: 'cyan', color: '#22d3ee' },
            { label: 'Win Rate', value: stats.win_rate + '%', accent: stats.win_rate >= 50 ? 'green' : 'red', color: stats.win_rate >= 50 ? '#4ade80' : '#f87171' },
            { label: 'Profit Factor', value: stats.profit_factor, accent: stats.profit_factor >= 1.5 ? 'green' : 'amber', color: stats.profit_factor >= 1.5 ? '#4ade80' : '#facc15' },
            { label: 'Total P&L', value: '$' + stats.total_profit.toFixed(2), accent: stats.total_profit >= 0 ? 'green' : 'red', color: stats.total_profit >= 0 ? '#4ade80' : '#f87171' },
            { label: 'BE Activation', value: beDisplay, accent: 'amber', color: '#facc15' },
            { label: 'Best Trade', value: '+$' + stats.best_trade_profit.toFixed(2), accent: 'green', color: '#4ade80' },
            { label: 'Worst Trade', value: '$' + stats.worst_trade_profit.toFixed(2), accent: 'red', color: '#f87171' },
            { label: 'Max Drawdown', value: '$' + stats.max_drawdown.toFixed(2), accent: 'red', color: '#f87171' },
        ];

        grid.innerHTML = cards.map(function(c) {
            return '<div class="hst-stat hst-stat-' + c.accent + '">' +
                '<div class="hst-stat-label">' + c.label + '</div>' +
                '<div class="hst-stat-value" style="color:' + c.color + '">' + c.value + '</div>' +
            '</div>';
        }).join('');
    }

    // ── Equity Curve (full width, gradient fill, colored markers) ──
    renderChart() {
        var ctx = document.getElementById('equityChart').getContext('2d');
        var curve = this.data.equity_curve || [];
        var trades = this.data.trades || [];
        var initialBalance = Number(this.data.initial_balance) || 1000;

        var dataPoints = [{x: 'Start', y: initialBalance}];
        curve.forEach(function(point) {
            var pt = String(point.time || '');
            if (pt && !/[Zz]|[+\-]\d{2}:?\d{2}$/.test(pt)) pt = pt + 'Z';
            var date = new Date(pt);
            var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][date.getMonth()];
            var label = mo + ' ' + date.getDate() + ' \u00B7 ' + date.getHours() + ':' + date.getMinutes().toString().padStart(2, '0');
            dataPoints.push({x: label, y: point.equity});
        });

        var labels = dataPoints.map(function(p) { return p.x; });
        var data = dataPoints.map(function(p) { return p.y; });

        // Point colors: green for equity increase, red for decrease
        var pointColors = data.map(function(val, i) {
            if (i === 0) return 'transparent';
            return val >= data[i-1] ? 'rgba(74,222,128,0.9)' : 'rgba(248,113,113,0.9)';
        });
        var pointRadii = data.map(function(val, i) {
            if (i === 0) return 0;
            return 3;
        });

        // Summary text
        var summaryEl = document.getElementById('equity-summary');
        if (summaryEl && data.length > 1) {
            var finalEq = data[data.length - 1];
            var change = finalEq - initialBalance;
            var sign = change >= 0 ? '+' : '';
            var clr = change >= 0 ? '#4ade80' : '#f87171';
            summaryEl.innerHTML = '<span style="color:#64748b">Balance:</span> <span style="color:' + clr + '">$' + finalEq.toFixed(2) + ' (' + sign + '$' + change.toFixed(2) + ')</span>';
        }

        if (this.chart) {
            this.chart.data.labels = labels;
            this.chart.data.datasets[0].data = data;
            this.chart.data.datasets[0].pointBackgroundColor = pointColors;
            this.chart.data.datasets[0].pointRadius = pointRadii;

            var finalEq2 = data[data.length - 1] || 0;
            var color2 = finalEq2 >= initialBalance ? 'rgb(74, 222, 128)' : 'rgb(248, 113, 113)';
            var bgGrad2 = ctx.createLinearGradient(0, 0, 0, 320);
            bgGrad2.addColorStop(0, 'rgba(74, 222, 128, 0.18)');
            bgGrad2.addColorStop(0.45, 'rgba(74, 222, 128, 0.04)');
            bgGrad2.addColorStop(0.55, 'rgba(248, 113, 113, 0.04)');
            bgGrad2.addColorStop(1, 'rgba(248, 113, 113, 0.12)');
            this.chart.data.datasets[0].borderColor = color2;
            this.chart.data.datasets[0].backgroundColor = bgGrad2;
            this.chart.update('none');
            return;
        }

        var finalEq = data[data.length - 1] || 0;
        var color = finalEq >= initialBalance ? 'rgb(74, 222, 128)' : 'rgb(248, 113, 113)';
        // Dual-tone gradient: green above initial balance, red below
        var bgGradient = ctx.createLinearGradient(0, 0, 0, 320);
        bgGradient.addColorStop(0, 'rgba(74, 222, 128, 0.18)');
        bgGradient.addColorStop(0.45, 'rgba(74, 222, 128, 0.04)');
        bgGradient.addColorStop(0.55, 'rgba(248, 113, 113, 0.04)');
        bgGradient.addColorStop(1, 'rgba(248, 113, 113, 0.12)');

        // Custom plugin: initial balance baseline dashed reference line
        var baselinePlugin = {
            id: 'baselineLine',
            afterDraw: function(chart) {
                var yScale = chart.scales.y;
                if (!yScale) return;
                var yPos = yScale.getPixelForValue(initialBalance);
                if (yPos < chart.chartArea.top || yPos > chart.chartArea.bottom) return;
                var ctx2 = chart.ctx;
                ctx2.save();
                ctx2.beginPath();
                ctx2.setLineDash([6, 4]);
                ctx2.strokeStyle = 'rgba(148, 163, 184, 0.3)';
                ctx2.lineWidth = 1;
                ctx2.moveTo(chart.chartArea.left, yPos);
                ctx2.lineTo(chart.chartArea.right, yPos);
                ctx2.stroke();
                ctx2.restore();
            }
        };

        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Account Balance ($)',
                    data: data,
                    borderColor: color,
                    backgroundColor: bgGradient,
                    borderWidth: 2,
                    pointRadius: pointRadii,
                    pointHoverRadius: 5,
                    pointBackgroundColor: pointColors,
                    pointBorderWidth: 0,
                    fill: true,
                    tension: 0.2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        backgroundColor: 'rgba(8, 8, 20, 0.95)',
                        titleColor: '#94a3b8',
                        titleFont: { family: 'JetBrains Mono', size: 10, weight: 700 },
                        bodyColor: '#e2e8f0',
                        bodyFont: { family: 'JetBrains Mono', size: 11, weight: 600 },
                        borderColor: 'rgba(255,255,255,0.1)',
                        borderWidth: 1,
                        padding: 10,
                        cornerRadius: 8,
                        callbacks: {
                            label: (function(anchor) {
                                return function(context) {
                                    var val = context.parsed.y;
                                    var change = val - anchor;
                                    var sign = change >= 0 ? '+' : '';
                                    return ' $' + val.toFixed(2) + '  (' + sign + '$' + change.toFixed(2) + ')';
                                };
                            })(initialBalance)
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
                        ticks: { color: '#475569', maxTicksLimit: 10, font: { family: 'JetBrains Mono', size: 9 } }
                    },
                    y: {
                        grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
                        min: 750,
                        ticks: {
                            color: '#475569',
                            font: { family: 'JetBrains Mono', size: 9 },
                            callback: function(value) { return '$' + value; }
                        }
                    }
                },
                interaction: { mode: 'nearest', axis: 'x', intersect: false }
            },
            plugins: [baselinePlugin]
        });
    }

    // ── Live vs Backtest Comparison Bars ──
    renderComparisonBars() {
        var live = this.data.live_stats || this.data.global_stats;
        var bt = this.backtestRef;
        var container = document.getElementById('comparison-bars');

        var metrics = [
            { label: 'Trades', bt: bt.trades, live: live.total_trades, fmt: function(v) { return Math.round(v); }, unit: '' },
            { label: 'Win Rate', bt: bt.winRate, live: live.win_rate, fmt: function(v) { return v.toFixed(1); }, unit: '%' },
            { label: 'Profit Factor', bt: bt.profitFactor, live: live.profit_factor, fmt: function(v) { return v.toFixed(2); }, unit: '' },
            { label: 'Max Drawdown', bt: bt.maxDrawdown, live: live.max_drawdown, fmt: function(v) { return '$' + v.toFixed(0); }, unit: '', inverted: true }
        ];

        container.innerHTML = metrics.map(function(m) {
            var diff = m.live - m.bt;
            var isGood = m.inverted ? diff <= 0 : diff >= 0;
            var clr = isGood ? '#4ade80' : '#f87171';
            var diffStr = (diff >= 0 ? '+' : '') + m.fmt(diff) + m.unit;
            var maxVal = Math.max(m.bt, m.live) || 1;
            var btPct = Math.min((m.bt / maxVal) * 100, 100);
            var livePct = Math.min((m.live / maxVal) * 100, 100);

            return '<div style="padding:12px 16px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px">' +
                '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">' +
                    '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b">' + m.label + '</span>' +
                    '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:800;color:' + clr + '">' + diffStr + '</span>' +
                '</div>' +
                '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
                    '<span style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#475569">BT: ' + m.fmt(m.bt) + m.unit + '</span>' +
                    '<span style="font-family:\'JetBrains Mono\',monospace;font-size:11px;font-weight:800;color:#e2e8f0">Live: ' + m.fmt(m.live) + m.unit + '</span>' +
                '</div>' +
                '<div class="hst-cmp-bar-track">' +
                    '<div class="hst-cmp-bar-fill" style="width:' + btPct + '%;background:rgba(100,116,139,0.3)"></div>' +
                '</div>' +
                '<div class="hst-cmp-bar-track" style="margin-top:3px">' +
                    '<div class="hst-cmp-bar-fill" style="width:' + livePct + '%;background:' + clr + '"></div>' +
                '</div>' +
            '</div>';
        }).join('');
    }

    // ── Monthly Summary ──
    renderMonthlyTable() {
        var tbody = document.getElementById('monthly-table-body');

        if (!this.data.monthly_stats || this.data.monthly_stats.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="padding:16px;text-align:center;color:#475569;font-family:\'JetBrains Mono\',monospace;font-size:10px">No monthly data</td></tr>';
            return;
        }

        // Find max abs P&L for bar scaling
        var maxPnl = 1;
        this.data.monthly_stats.forEach(function(ms) { maxPnl = Math.max(maxPnl, Math.abs(ms.profit)); });

        tbody.innerHTML = this.data.monthly_stats.map(function(ms) {
            var pnlColor = ms.profit >= 0 ? '#4ade80' : '#f87171';
            var rowBg = ms.profit >= 0 ? 'rgba(74,222,128,0.02)' : 'rgba(248,113,113,0.02)';
            var wrColor = ms.win_rate >= 60 ? '#4ade80' : ms.win_rate >= 45 ? '#facc15' : '#f87171';
            var pfColor = ms.profit_factor >= 1.5 ? '#4ade80' : ms.profit_factor >= 1.0 ? '#facc15' : '#f87171';
            var barWidth = Math.round((Math.abs(ms.profit) / maxPnl) * 60);

            // Format month name
            var parts = ms.month.split('-');
            var moNames = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            var moLabel = moNames[parseInt(parts[1])] + ' ' + parts[0];

            return '<tr class="hst-monthly-row" style="font-family:\'JetBrains Mono\',monospace;font-size:11px;border-bottom:1px solid rgba(255,255,255,0.04);background:' + rowBg + '">' +
                '<td style="padding:10px 12px;font-weight:700;color:#e2e8f0;letter-spacing:0.05em">' + moLabel + '</td>' +
                '<td style="padding:10px 12px;text-align:center;color:#94a3b8;font-weight:600">' + ms.trades + '</td>' +
                '<td style="padding:10px 12px;text-align:center;font-weight:600">' +
                    '<span style="color:#4ade80">' + ms.wins + '</span>' +
                    '<span style="color:#334155"> / </span>' +
                    '<span style="color:#f87171">' + ms.losses + '</span>' +
                    '<span style="color:#334155"> / </span>' +
                    '<span style="color:#475569">' + ms.breakevens + '</span>' +
                '</td>' +
                '<td style="padding:10px 12px;text-align:center;font-weight:700;color:' + wrColor + '">' + ms.win_rate.toFixed(1) + '%</td>' +
                '<td style="padding:10px 12px;text-align:right;font-weight:800;color:' + pnlColor + '">' +
                    (ms.profit >= 0 ? '+' : '') + '$' + ms.profit.toFixed(2) +
                    '<span class="hst-pnl-bar" style="width:' + barWidth + 'px;background:' + pnlColor + '"></span>' +
                '</td>' +
                '<td style="padding:10px 12px;text-align:center;font-weight:700;color:' + pfColor + '">' + ms.profit_factor.toFixed(2) + '</td>' +
                '<td style="padding:10px 12px;text-align:right;font-weight:600;color:#f87171">$' + ms.max_drawdown.toFixed(2) + '</td>' +
            '</tr>';
        }).join('');
    }

    // ── Trade Table (compact columns, expandable rows) ──
    renderTradesTable() {
        var tbody = document.getElementById('trades-table-body');
        var trades = this.data.trades || [];
        var self = this;

        var totalPages = Math.max(1, Math.ceil(trades.length / this.pageSize));
        if (this.currentPage > totalPages) this.currentPage = totalPages;
        if (this.currentPage < 1) this.currentPage = 1;

        document.getElementById('current-page').textContent = this.currentPage;
        document.getElementById('total-pages').textContent = totalPages;
        document.getElementById('btn-prev').disabled = this.currentPage === 1;
        document.getElementById('btn-next').disabled = this.currentPage === totalPages;

        var startIdx = (this.currentPage - 1) * this.pageSize;
        var pageTrades = trades.slice(startIdx, startIdx + this.pageSize);

        if (pageTrades.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="padding:16px;text-align:center;color:#475569;font-family:\'JetBrains Mono\',monospace;font-size:10px">No trades recorded yet</td></tr>';
            return;
        }

        var html = '';
        pageTrades.forEach(function(t) {
            var dirColor = t.direction === 'BUY' ? '#4ade80' : '#f87171';
            var dirBg = t.direction === 'BUY' ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)';
            var dirBorder = t.direction === 'BUY' ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.2)';

            var pnl = parseFloat(t.profit) || 0;
            var pnlColor = pnl > 0.5 ? '#4ade80' : (pnl < -0.5 ? '#f87171' : '#64748b');
            var pnlStr = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
            var pipsStr = t.pips ? (t.pips >= 0 ? '+' : '') + t.pips.toFixed(1) + 'p' : '';

            // Compact date: "Apr 2 · 4:59 PM" — defensive: ensure Z suffix so browser parses as UTC, not local
            var dateStr = '--';
            if (t.open_time) {
                var openStr = String(t.open_time);
                if (!/[Zz]|[+\-]\d{2}:?\d{2}$/.test(openStr)) openStr = openStr + 'Z';
                var d = new Date(openStr);
                var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
                var hr = d.getHours();
                var ampm = hr >= 12 ? 'PM' : 'AM';
                hr = hr % 12 || 12;
                dateStr = mo + ' ' + d.getDate() + ' \u00B7 ' + hr + ':' + d.getMinutes().toString().padStart(2,'0') + ' ' + ampm;
            }

            // Price: "4663 → 4660"
            var priceStr = (t.open_price ? t.open_price.toFixed(2) : '--') + ' \u2192 ' + (t.close_price ? t.close_price.toFixed(2) : '--');

            // Confidence
            var confStr = t.confidence ? t.confidence.toFixed(0) + '%' : '--';
            var confColor = t.confidence >= 70 ? '#22d3ee' : (t.confidence >= 40 ? '#94a3b8' : '#64748b');

            // Scenario
            var scenario = self.scenarioLabel(t);

            // Close reason badge
            var resultBadge = self.renderResultBadge(t);

            // Ticket for expand
            var ticket = t.ticket || 0;
            var isExpanded = self.expandedTicket === ticket;

            html += '<tr class="hst-trade-row" style="font-family:\'JetBrains Mono\',monospace;font-size:10px;border-bottom:1px solid rgba(255,255,255,0.04)" onclick="historyApp.toggleExpand(' + ticket + ')">' +
                '<td style="padding:10px 12px;color:#94a3b8;font-weight:600;white-space:nowrap">' + dateStr + '</td>' +
                '<td style="padding:10px 12px"><span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:9px;font-weight:800;color:' + dirColor + ';background:' + dirBg + ';border:1px solid ' + dirBorder + '">' + t.direction + '</span></td>' +
                '<td style="padding:10px 12px;color:#cbd5e1;font-weight:600;white-space:nowrap">' + priceStr + '</td>' +
                '<td style="padding:10px 12px;text-align:right;font-weight:800;color:' + pnlColor + ';white-space:nowrap">' + pnlStr + ' <span style="color:#475569;font-weight:600;font-size:9px">' + pipsStr + '</span></td>' +
                '<td style="padding:10px 12px;text-align:right;color:' + confColor + ';font-weight:700">' + confStr + '</td>' +
                '<td style="padding:10px 12px;color:#64748b;font-weight:600;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + scenario + '</td>' +
                '<td style="padding:10px 12px;white-space:nowrap">' + resultBadge + '</td>' +
            '</tr>';

            // Expandable detail row
            var dur = t.duration_minutes ? (t.duration_minutes < 60 ? t.duration_minutes + 'm' : Math.floor(t.duration_minutes/60) + 'h ' + (t.duration_minutes%60) + 'm') : '--';
            var reportBtn = ticket ? '<button style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.1em;padding:4px 12px;background:rgba(34,211,238,0.08);border:1px solid rgba(34,211,238,0.2);border-radius:6px;color:#22d3ee;cursor:pointer" onclick="event.stopPropagation();historyApp.openTradeReport(' + ticket + ')">View Report</button>' : '';

            html += '<tr class="hst-trade-expand' + (isExpanded ? ' open' : '') + '" id="expand-' + ticket + '">' +
                '<td colspan="7" style="padding:12px 16px">' +
                    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;font-family:\'JetBrains Mono\',monospace;font-size:10px">' +
                        '<div><span style="color:#475569;font-weight:700">Entry</span><div style="color:#e2e8f0;font-weight:600;margin-top:2px">' + (t.open_price ? t.open_price.toFixed(2) : '--') + '</div></div>' +
                        '<div><span style="color:#475569;font-weight:700">Exit</span><div style="color:#e2e8f0;font-weight:600;margin-top:2px">' + (t.close_price ? t.close_price.toFixed(2) : '--') + '</div></div>' +
                        '<div><span style="color:#475569;font-weight:700">SL</span><div style="color:#f87171;font-weight:600;margin-top:2px">' + (t.sl ? t.sl.toFixed(2) : '--') + '</div></div>' +
                        '<div><span style="color:#475569;font-weight:700">TP</span><div style="color:#4ade80;font-weight:600;margin-top:2px">' + (t.tp ? t.tp.toFixed(2) : '--') + '</div></div>' +
                        '<div><span style="color:#475569;font-weight:700">Duration</span><div style="color:#94a3b8;font-weight:600;margin-top:2px">' + dur + '</div></div>' +
                        '<div><span style="color:#475569;font-weight:700">BE</span><div style="font-weight:600;margin-top:2px">' + self.renderBeBadge(t.breakeven_activated) + '</div></div>' +
                    '</div>' +
                    (t.scenario_description ? '<div style="margin-top:10px;font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;line-height:1.5">' + self.escapeHtml(t.scenario_description) + '</div>' : '') +
                    (reportBtn ? '<div style="margin-top:10px">' + reportBtn + '</div>' : '') +
                '</td>' +
            '</tr>';
        });

        tbody.innerHTML = html;

        // Render pagination numbers
        this.renderPaginationNumbers(totalPages);
    }

    renderPaginationNumbers(totalPages) {
        var container = document.getElementById('pagination-numbers');
        if (!container) return;
        var html = '';
        var self = this;
        for (var i = 1; i <= totalPages; i++) {
            var isActive = i === this.currentPage;
            html += '<button onclick="historyApp.goToPage(' + i + ')" style="width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;border:1px solid ' + (isActive ? 'rgba(74,222,128,0.4)' : 'rgba(255,255,255,0.06)') + ';background:' + (isActive ? 'rgba(74,222,128,0.1)' : 'transparent') + ';color:' + (isActive ? '#4ade80' : '#64748b') + ';cursor:pointer;font-size:10px;font-weight:700">' + i + '</button>';
        }
        container.innerHTML = html;
    }

    toggleExpand(ticket) {
        var el = document.getElementById('expand-' + ticket);
        if (!el) return;
        if (this.expandedTicket === ticket) {
            el.classList.remove('open');
            this.expandedTicket = null;
        } else {
            // Close previous
            if (this.expandedTicket) {
                var prev = document.getElementById('expand-' + this.expandedTicket);
                if (prev) prev.classList.remove('open');
            }
            el.classList.add('open');
            this.expandedTicket = ticket;
        }
    }

    goToPage(page) {
        this.currentPage = page;
        this.renderTradesTable();
    }

    renderResultBadge(t) {
        var pnl = parseFloat(t.profit) || 0;
        var cr = (t.close_reason || '').toLowerCase();

        if (cr.includes('stop loss') || cr.includes('sl hit')) {
            if (pnl > 0) {
                return '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:9px;font-weight:800;color:#60a5fa;background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.2)">TRAILING</span>';
            } else if (pnl > -1.0) {
                return '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:9px;font-weight:800;color:#94a3b8;background:rgba(148,163,184,0.1);border:1px solid rgba(148,163,184,0.15)">FLAT</span>';
            } else {
                return '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:9px;font-weight:800;color:#f87171;background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.2)">SL</span>';
            }
        } else if (cr.includes('take profit') || cr.includes('tp hit')) {
            return '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:9px;font-weight:800;color:#4ade80;background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.2)">TP</span>';
        }

        // Fallback: show full close reason
        var label = t.close_reason || 'Unknown';
        if (label.length > 20) label = label.substring(0, 18) + '..';
        return '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:9px;font-weight:700;color:#64748b;background:rgba(100,116,139,0.1);border:1px solid rgba(100,116,139,0.15)">' + this.escapeHtml(label) + '</span>';
    }

    escapeHtml(s) {
        var str = (s === null || s === undefined) ? '' : String(s);
        // FLO-291: also replace snake_case tokens with spaces so backend
        // identifiers (e.g. "dollar_gold_correlation_break") don't render
        // with visual underline-like artifacts in monospace.
        str = str.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b/gi, function (m) {
            return m.replace(/_/g, ' ');
        });
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ── Sorting ──
    sortTrades(column) {
        if (this.currentSort.column === column) {
            this.currentSort.direction = this.currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
            this.currentSort.column = column;
            this.currentSort.direction = 'desc';
        }
        this.sortData();
        this.currentPage = 1;
        this.renderTradesTable();
    }

    sortData() {
        if (!this.data || !this.data.trades) return;
        var column = this.currentSort.column;
        var dirMult = this.currentSort.direction === 'asc' ? 1 : -1;

        this.data.trades.sort(function(a, b) {
            var valA = a[column];
            var valB = b[column];
            if (valA === null) valA = '';
            if (valB === null) valB = '';

            if (typeof valA === 'string' && typeof valB === 'string') {
                if (column.includes('time')) {
                    return (new Date(valA) - new Date(valB)) * dirMult;
                }
                return valA.localeCompare(valB) * dirMult;
            } else {
                return (valA - valB) * dirMult;
            }
        });
    }

    formatDate(isoString) {
        if (!isoString) return '--';
        if (window.displayTime) return window.displayTime(isoString);
        var d = new Date(isoString);
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
    }

    renderBeBadge(beActivated) {
        if (beActivated === true) {
            return '<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:9px;font-weight:800;color:#4ade80;background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.2)">\u2713</span>';
        } else if (beActivated === false) {
            return '<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:9px;font-weight:800;color:#64748b;background:rgba(100,116,139,0.1);border:1px solid rgba(100,116,139,0.1)">\u2014</span>';
        } else {
            return '<span style="font-size:9px;color:#334155;font-weight:700">?</span>';
        }
    }

    scenarioLabel(trade) {
        var key = trade ? trade.scenario : null;
        var map = {
            'momentum_forte_confirmado': 'Strong momentum',
            'rsi_extremo_com_momentum': 'RSI extreme + momentum',
            'divergencia_tecnica': 'Technical divergence',
            'breakout_confirmado': 'Confirmed breakout',
            'lateralizacao': 'Ranging',
            'sinais_conflitantes': 'Conflicting signals',
            'ml_vs_tech_conflito': 'ML vs Tech conflict',
            'alinhamento_perfeito': 'Perfect alignment',
            'janela_pos_evento': 'Post-event window',
            'volatilidade_extrema': 'Extreme volatility',
            'zona_sr_forte': 'Near strong S/R zone',
            'confluence': 'Confluence',
            'padrao': 'Default',
        };
        if (key && map[key]) return map[key];
        if (key) return String(key).replace(/_/g, ' ');
        return '--';
    }

    // ── Modal (unchanged) ──
    openTradeReport(ticket) {
        if (!ticket) return;
        var modal = document.getElementById('trade-report-modal');
        var body = document.getElementById('trade-report-body');
        var meta = document.getElementById('trade-report-meta');
        if (!modal || !body) return;
        this.modalOpen = true;
        modal.classList.remove('hidden');
        if (meta) meta.textContent = 'Ticket #' + ticket + ' \u2014 Loading...';
        body.innerHTML = '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#64748b">Fetching report...</div>';
        this.fetchTradeReport(ticket);
    }

    closeTradeReport() {
        var modal = document.getElementById('trade-report-modal');
        if (!modal) return;
        this.modalOpen = false;
        modal.classList.add('hidden');
    }

    async fetchTradeReport(ticket) {
        var body = document.getElementById('trade-report-body');
        var meta = document.getElementById('trade-report-meta');
        if (!body) return;
        var self = this;

        try {
            var response = await fetch('/api/trade-report?ticket=' + encodeURIComponent(ticket));
            var result = null;
            try { result = await response.json(); } catch (_) { result = null; }

            if (!response.ok || !result || result.ok !== true) {
                var err = (result && (result.error || result.detail || result.message)) ? (result.error || result.detail || result.message) : 'http_' + response.status;
                if (meta) meta.textContent = 'Ticket #' + ticket;
                body.innerHTML = '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#f87171">Report unavailable: ' + String(err) + '</div>';
                return;
            }

            var createdAt = result.created_at ? (window.displayTime ? window.displayTime(result.created_at) : new Date(result.created_at).toLocaleString()) : '--';
            var cached = result.cached === true;
            var model = result.model || '';
            var report = result.report || {};

            if (meta) {
                meta.textContent = 'Ticket #' + ticket + ' \u2014 ' + (cached ? 'CACHED' : 'NEW') + ' \u2014 ' + createdAt + (model ? ' \u2014 ' + model : '');
            }

            var list = function(arr) {
                if (!arr || arr.length === 0) return '<div style="font-size:10px;color:#334155">\u2014</div>';
                return '<ul style="margin-top:4px;list-style:none;padding:0">' + arr.map(function(x) {
                    return '<li style="font-size:11px;color:#cbd5e1;font-weight:600;line-height:1.6;padding:2px 0">\u2022 ' + self.escapeHtml(x) + '</li>';
                }).join('') + '</ul>';
            };

            body.innerHTML = '<div style="display:flex;flex-direction:column;gap:16px">' +
                '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b">Summary</div>' +
                '<div style="font-size:13px;color:#e2e8f0;margin-top:6px;line-height:1.6;font-weight:600">' + self.escapeHtml(report.summary || '\u2014') + '</div></div>' +
                '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">' +
                    '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b">What went well</div>' + list(report.what_went_well) + '</div>' +
                    '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b">What went wrong</div>' + list(report.what_went_wrong) + '</div>' +
                '</div>' +
                '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">' +
                    '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b">Key risks</div>' + list(report.key_risks_observed) + '</div>' +
                    '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b">Improvements</div>' + list(report.suggested_improvements) + '</div>' +
                '</div>' +
                '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#475569">Confidence: <span style="color:#94a3b8;font-weight:700">' + String(report.confidence_in_assessment || 'medium').toUpperCase() + '</span></div>' +
            '</div>';
        } catch (e) {
            if (meta) meta.textContent = 'Ticket #' + ticket;
            body.innerHTML = '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#f87171">Report unavailable: ' + String(e) + '</div>';
        }
    }

    nextPage() { this.currentPage++; this.renderTradesTable(); }
    prevPage() { this.currentPage--; this.renderTradesTable(); }
}

// Init
var historyApp = new HistoryApp();
document.addEventListener('DOMContentLoaded', function() {
    historyApp.init();
});
