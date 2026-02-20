// history.js - Trade History & Performance Dashboard

class HistoryApp {
    constructor() {
        this.data = null;
        this.currentPage = 1;
        this.pageSize = 20;
        this.currentSort = { column: 'close_time', direction: 'desc' };
        this.chart = null;
        
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
    }

    async fetchData() {
        try {
            document.getElementById('loading-indicator').classList.remove('hidden');
            const response = await fetch('/api/history-data');
            
            if (!response.ok) throw new Error('Network response was not ok');
            
            const result = await response.json();
            if (result.error) throw new Error(result.error);
            
            this.data = result;
            
            // Reapply sort if not the default
            if (this.currentSort.column !== 'close_time' || this.currentSort.direction !== 'desc') {
                this.sortData();
            } else {
                // Ensure default sort is descending by close_time
                this.data.trades.sort((a, b) => new Date(b.close_time) - new Date(a.close_time));
            }
            
            this.render();
            document.getElementById('last-updated').textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
            document.getElementById('loading-indicator').classList.add('hidden');
        } catch (error) {
            console.error('Error fetching history data:', error);
            document.getElementById('last-updated').textContent = `Error fetching data: ${error.message}`;
            document.getElementById('loading-indicator').classList.add('hidden');
        }
    }

    startPolling() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.pollTimer = setInterval(() => this.fetchData(), this.pollingInterval);
    }

    render() {
        if (!this.data) return;
        
        this.renderGlobalStats();
        this.renderMonthlyTable();
        this.renderComparisonTable();
        this.renderChart();
        this.renderTradesTable();
    }

    renderGlobalStats() {
        const stats = this.data.global_stats;
        const grid = document.getElementById('global-stats-grid');
        
        const cards = [
            { label: 'Total Trades', value: stats.total_trades, icon: '📊' },
            { label: 'Win Rate', value: `${stats.win_rate}%`, icon: '🎯', color: stats.win_rate >= 50 ? 'text-green-400' : 'text-red-400' },
            { label: 'Profit Factor', value: stats.profit_factor, icon: '⚖️', color: stats.profit_factor >= 1.5 ? 'text-green-400' : 'text-amber-400' },
            { label: 'Total P&L', value: `$${stats.total_profit.toFixed(2)}`, icon: '💰', color: stats.total_profit >= 0 ? 'text-green-400' : 'text-red-400' },
            { label: 'Avg Win / Loss', value: `$${stats.avg_win.toFixed(2)} / $${stats.avg_loss.toFixed(2)}`, icon: '↕️' },
            { label: 'Best Trade', value: `$${stats.best_trade_profit.toFixed(2)}`, icon: '🏆', color: 'text-green-400' },
            { label: 'Worst Trade', value: `$${stats.worst_trade_profit.toFixed(2)}`, icon: '💔', color: 'text-red-400' },
            { label: 'Max Drawdown', value: `$${stats.max_drawdown.toFixed(2)}`, icon: '📉', color: 'text-red-400' },
        ];
        
        grid.innerHTML = cards.map(c => `
            <div class="about-card p-4 flex flex-col justify-center items-center text-center">
                <div class="text-xs text-gray-500 tracking-widest mb-1 flex items-center gap-1">
                    <span>${c.icon}</span> ${c.label}
                </div>
                <div class="text-xl sm:text-2xl font-bold ${c.color || 'text-gray-200'}">${c.value}</div>
            </div>
        `).join('');
    }

    renderMonthlyTable() {
        const tbody = document.getElementById('monthly-table-body');
        
        if (!this.data.monthly_stats || this.data.monthly_stats.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="p-4 text-center text-gray-500">No monthly data available</td></tr>`;
            return;
        }
        
        tbody.innerHTML = this.data.monthly_stats.map(ms => {
            const pnlColor = ms.profit >= 0 ? 'text-green-400' : 'text-red-400';
            return `
            <tr class="hover:bg-gray-800/30 transition-colors">
                <td class="p-3 text-gray-300 font-medium">${ms.month}</td>
                <td class="p-3 text-right text-gray-400">${ms.trades}</td>
                <td class="p-3 text-right">
                    <span class="text-green-400">${ms.wins}</span> / 
                    <span class="text-red-400">${ms.losses}</span> / 
                    <span class="text-gray-500">${ms.breakevens}</span>
                </td>
                <td class="p-3 text-right text-gray-300">${ms.win_rate.toFixed(1)}%</td>
                <td class="p-3 text-right font-medium ${pnlColor}">$${ms.profit.toFixed(2)}</td>
                <td class="p-3 text-right text-gray-300">${ms.profit_factor.toFixed(2)}</td>
                <td class="p-3 text-right text-red-400">$${ms.max_drawdown.toFixed(2)}</td>
            </tr>
            `;
        }).join('');
    }

    renderComparisonTable() {
        const live = this.data.global_stats;
        const bt = this.backtestRef;
        const tbody = document.getElementById('comparison-table-body');
        
        const diffColor = (val, inverted = false) => {
            if (val === 0) return 'text-gray-500';
            const isGood = inverted ? val < 0 : val > 0;
            return isGood ? 'text-green-400' : 'text-red-400';
        };
        
        const diffSign = (val) => val > 0 ? '+' : '';

        const rows = [
            { label: 'Trades', bt: bt.trades, live: live.total_trades, diff: live.total_trades - bt.trades, format: v => v },
            { label: 'Win Rate (%)', bt: bt.winRate, live: live.win_rate, diff: live.win_rate - bt.winRate, format: v => v.toFixed(1) },
            { label: 'Profit Factor', bt: bt.profitFactor, live: live.profit_factor, diff: live.profit_factor - bt.profitFactor, format: v => v.toFixed(2) },
            { label: 'Max Drawdown ($)', bt: bt.maxDrawdown, live: live.max_drawdown, diff: live.max_drawdown - bt.maxDrawdown, format: v => v.toFixed(2), inverted: true }
        ];

        tbody.innerHTML = rows.map(r => `
            <tr>
                <td class="py-2 text-gray-400">${r.label}</td>
                <td class="py-2 text-right text-gray-500">${r.format(r.bt)}</td>
                <td class="py-2 text-right text-gray-200 font-medium">${r.format(r.live)}</td>
                <!--<td class="py-2 text-right ${diffColor(r.diff, r.inverted)} text-xs">${diffSign(r.diff)}${r.format(r.diff)}</td>-->
            </tr>
        `).join('');
    }

    renderChart() {
        const ctx = document.getElementById('equityChart').getContext('2d');
        const curve = this.data.equity_curve || [];
        
        // Add starting point 0
        const dataPoints = [{x: 'Start', y: 0}];
        curve.forEach((point, index) => {
            // Shorten the time label for X axis
            const date = new Date(point.time);
            const label = `${date.getMonth()+1}/${date.getDate()} ${date.getHours()}:${date.getMinutes().toString().padStart(2, '0')}`;
            dataPoints.push({x: label, y: point.equity});
        });

        const labels = dataPoints.map(p => p.x);
        const data = dataPoints.map(p => p.y);

        if (this.chart) {
            this.chart.data.labels = labels;
            this.chart.data.datasets[0].data = data;
            
            // Update color based on final equity
            const finalEq = data[data.length - 1] || 0;
            const color = finalEq >= 0 ? 'rgb(74, 222, 128)' : 'rgb(248, 113, 113)';
            const bgGradient = ctx.createLinearGradient(0, 0, 0, 400);
            bgGradient.addColorStop(0, finalEq >= 0 ? 'rgba(74, 222, 128, 0.2)' : 'rgba(248, 113, 113, 0.2)');
            bgGradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
            
            this.chart.data.datasets[0].borderColor = color;
            this.chart.data.datasets[0].backgroundColor = bgGradient;
            
            this.chart.update('none'); // Update without animation for polling
            return;
        }

        // Determine initial colors
        const finalEq = data[data.length - 1] || 0;
        const color = finalEq >= 0 ? 'rgb(74, 222, 128)' : 'rgb(248, 113, 113)';
        const bgGradient = ctx.createLinearGradient(0, 0, 0, 400);
        bgGradient.addColorStop(0, finalEq >= 0 ? 'rgba(74, 222, 128, 0.2)' : 'rgba(248, 113, 113, 0.2)');
        bgGradient.addColorStop(1, 'rgba(0, 0, 0, 0)');

        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Cumulative P&L ($)',
                    data: data,
                    borderColor: color,
                    backgroundColor: bgGradient,
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    fill: true,
                    tension: 0.1
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
                        backgroundColor: 'rgba(17, 24, 39, 0.9)',
                        titleColor: '#9ca3af',
                        bodyColor: '#e5e7eb',
                        borderColor: '#374151',
                        borderWidth: 1,
                        callbacks: {
                            label: function(context) {
                                let val = context.parsed.y;
                                return ` P&L: $${val.toFixed(2)}`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: '#1f2937', drawBorder: false },
                        ticks: { color: '#6b7280', maxTicksLimit: 8 }
                    },
                    y: {
                        grid: { color: '#1f2937', drawBorder: false },
                        ticks: {
                            color: '#6b7280',
                            callback: function(value) { return '$' + value; }
                        }
                    }
                },
                interaction: {
                    mode: 'nearest',
                    axis: 'x',
                    intersect: false
                }
            }
        });
    }

    sortTrades(column) {
        if (this.currentSort.column === column) {
            // Toggle direction
            this.currentSort.direction = this.currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
            // New column, default to desc
            this.currentSort.column = column;
            this.currentSort.direction = 'desc';
        }
        
        this.sortData();
        this.currentPage = 1;
        this.renderTradesTable();
    }
    
    sortData() {
        if (!this.data || !this.data.trades) return;
        
        const { column, direction } = this.currentSort;
        const dirMult = direction === 'asc' ? 1 : -1;
        
        this.data.trades.sort((a, b) => {
            let valA = a[column];
            let valB = b[column];
            
            // Handle nulls
            if (valA === null) valA = '';
            if (valB === null) valB = '';
            
            // Type specific sorting
            if (typeof valA === 'string' && typeof valB === 'string') {
                // Dates
                if (column.includes('time')) {
                    return (new Date(valA) - new Date(valB)) * dirMult;
                }
                return valA.localeCompare(valB) * dirMult;
            } else {
                // Numbers
                return (valA - valB) * dirMult;
            }
        });
    }

    formatDate(isoString) {
        if (!isoString) return '--';
        const d = new Date(isoString);
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
    }

    renderTradesTable() {
        const tbody = document.getElementById('trades-table-body');
        const trades = this.data.trades || [];
        
        const totalPages = Math.max(1, Math.ceil(trades.length / this.pageSize));
        
        // Bound current page
        if (this.currentPage > totalPages) this.currentPage = totalPages;
        if (this.currentPage < 1) this.currentPage = 1;
        
        document.getElementById('current-page').textContent = this.currentPage;
        document.getElementById('total-pages').textContent = totalPages;
        
        document.getElementById('btn-prev').disabled = this.currentPage === 1;
        document.getElementById('btn-next').disabled = this.currentPage === totalPages;
        
        // Slice for current page
        const startIdx = (this.currentPage - 1) * this.pageSize;
        const pageTrades = trades.slice(startIdx, startIdx + this.pageSize);
        
        if (pageTrades.length === 0) {
            tbody.innerHTML = `<tr><td colspan="10" class="p-4 text-center text-gray-500">No trades recorded yet</td></tr>`;
            return;
        }

        tbody.innerHTML = pageTrades.map(t => {
            const dirClass = t.direction === 'BUY' ? 'text-green-400 bg-green-400/10 border border-green-400/20' : 'text-red-400 bg-red-400/10 border border-red-400/20';
            
            const pnl = parseFloat(t.profit) || 0;
            let pnlClass = 'text-gray-500';
            if (pnl > 0.5) pnlClass = 'text-green-400';
            if (pnl < -0.5) pnlClass = 'text-red-400';
            
            // Format Close Reason
            let reasonBadge = `<span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold text-gray-400 bg-gray-800">${t.close_reason || 'Unknown'}</span>`;
            const cr = (t.close_reason || '').toLowerCase();
            
            if (cr.includes('stop loss') || cr.includes('sl hit')) {
                if (pnl > 0) {
                     reasonBadge = `<span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold text-blue-400 bg-blue-400/10 border border-blue-400/20">Trailing</span>`;
                } else if (pnl > -1.0) {
                     reasonBadge = `<span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold text-gray-400 bg-gray-400/10 border border-gray-400/20">Breakeven</span>`;
                } else {
                     reasonBadge = `<span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold text-red-400 bg-red-400/10 border border-red-400/20">SL</span>`;
                }
            } else if (cr.includes('take profit') || cr.includes('tp hit')) {
                reasonBadge = `<span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold text-green-400 bg-green-400/10 border border-green-400/20">TP</span>`;
            }

            return `
            <tr class="hover:bg-gray-800/30 transition-colors">
                <td class="p-3 text-gray-400 text-xs">
                    <div>${this.formatDate(t.open_time)}</div>
                    <!--<div class="text-gray-600 mt-0.5 text-[10px]">${this.formatDate(t.close_time)}</div>-->
                </td>
                <td class="p-3"><span class="px-2 py-0.5 rounded text-xs font-bold ${dirClass}">${t.direction}</span></td>
                <td class="p-3 text-right text-gray-300 font-mono text-xs">${t.open_price ? t.open_price.toFixed(2) : '--'}</td>
                <td class="p-3 text-right text-gray-300 font-mono text-xs">${t.close_price ? t.close_price.toFixed(2) : '--'}</td>
                <td class="p-3 text-right font-medium ${pnlClass}">$${pnl.toFixed(2)}</td>
                <td class="p-3 text-right text-gray-400">${t.pips ? t.pips.toFixed(1) : '--'}</td>
                <td class="p-3 text-right text-gray-400">${t.duration_minutes || '--'}</td>
                <td class="p-3 text-right text-cyan-400">${t.confidence ? t.confidence.toFixed(1) + '%' : '--'}</td>
                <td class="p-3 text-gray-400 text-xs">${t.scenario ? t.scenario.replace(/_/g, ' ') : '--'}</td>
                <td class="p-3">${reasonBadge}</td>
            </tr>
            `;
        }).join('');
    }

    nextPage() {
        this.currentPage++;
        this.renderTradesTable();
    }

    prevPage() {
        this.currentPage--;
        this.renderTradesTable();
    }
}

// Init
const historyApp = new HistoryApp();
document.addEventListener('DOMContentLoaded', () => {
    historyApp.init();
});
