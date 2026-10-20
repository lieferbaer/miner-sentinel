/**
 * Analytics — account figures on top (money lives at account level only),
 * then one full block per device, then a device-by-device comparison table.
 * Nothing is averaged across devices.
 */
import { DashboardSkeleton, formatDifficulty, formatHashrate, formatNumber } from '@/components/dashboard'
import MakeBadge from '@/components/devices/MakeBadge'
import EmptyState from '@/components/feedback/EmptyState'
import ErrorState from '@/components/feedback/ErrorState'
import DataFreshness from '@/components/metrics/DataFreshness'
import MetricCard from '@/components/metrics/MetricCard'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { useTimeRange } from '@/lib/TimeRangeContext'
import { formatRangeWindow, getChartTimeAxisConfig, toAnalyticsParams } from '@/lib/timeRange'
import { Activity, Award, Coins, Flame, Gauge, Hash, Server, Zap } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const REFRESH_MS = 60000

const GRANULARITY_LABEL = {
  raw: 'raw samples (10 s)',
  hour: 'hourly means',
  day: 'daily means',
}

function money(value, currency, decimals = 2) {
  if (value === null || value === undefined) return '—'
  return formatNumber(value, decimals) + ' ' + (currency === 'EUR' ? '€' : currency)
}

function btc(value) {
  if (value === null || value === undefined) return '—'
  return formatNumber(value, 8) + ' BTC'
}

function relativeTime(unixSeconds) {
  if (!unixSeconds) return '—'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - unixSeconds))
  if (seconds < 60) return seconds + ' s ago'
  if (seconds < 3600) return Math.floor(seconds / 60) + ' min ago'
  return Math.floor(seconds / 3600) + ' h ago'
}

function AccountCard({ account, currency, btcPrice }) {
  const toFiat = (value) =>
    value === null || value === undefined || !btcPrice ? null : value * btcPrice

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base font-semibold">
              {account.account}
              <Badge variant="outline" className="ml-2 font-normal">
                {account.pool_type}
              </Badge>
            </CardTitle>
            <CardDescription className="text-xs">
              Account level · {account.workers || 0} worker(s) reported by the pool
            </CardDescription>
          </div>
          <div className="text-right">
            <p className="text-[11px] text-muted-foreground">Pool-side hashrate</p>
            <p className="text-xl font-bold tabular-metrics">
              {formatHashrate(account.hashrate_ghs || 0)}
            </p>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
          {[
            { label: '1 h', value: formatHashrate(account.hashrate_1h_ghs || 0) },
            { label: '24 h', value: formatHashrate(account.hashrate_1d_ghs || 0) },
            { label: 'Balance', value: btc(account.balance_btc), fiat: toFiat(account.balance_btc) },
            {
              label: 'Today (est.)',
              value: btc(account.estimated_today_income_btc),
              fiat: toFiat(account.estimated_today_income_btc),
            },
            {
              label: 'Yesterday',
              value: btc(account.yesterday_income_btc),
              fiat: toFiat(account.yesterday_income_btc),
            },
            { label: 'Paid out', value: btc(account.paid_btc), fiat: toFiat(account.paid_btc) },
          ].map((entry) => (
            <div key={entry.label} className="rounded-lg bg-muted/40 px-3 py-2">
              <p className="text-[11px] text-muted-foreground">{entry.label}</p>
              <p className="truncate text-sm font-semibold tabular-metrics">{entry.value}</p>
              {entry.fiat !== undefined && entry.fiat !== null && (
                <p className="text-[11px] text-muted-foreground">{money(entry.fiat, currency)}</p>
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

function DeviceBlock({ entry, currency, granularity, rangeHours, rangeLabel }) {
  const device = entry.device
  const summary = entry.summary
  const pool = entry.pool
  const timeAxis = getChartTimeAxisConfig(rangeHours)
  const series = entry.series || []

  // eslint-disable-next-line react/display-name -- render function for the Recharts tooltip, not a component
  const tooltip = (rows) => (props) => {
    const { active, payload, label } = props
    if (!active || !payload || payload.length === 0) return null
    return (
      <div className="rounded-lg border bg-background/95 p-3 shadow-lg backdrop-blur">
        <div className="mb-2 text-xs text-muted-foreground">{timeAxis.tooltip(label)}</div>
        {rows.map((row) => {
          const item = payload.find((candidate) => candidate.dataKey === row.key)
          if (!item || item.value === null || item.value === undefined) return null
          return (
            <div key={row.key} className="flex items-center justify-between gap-4 text-xs">
              <span className="text-muted-foreground">{row.label}</span>
              <span className="font-semibold tabular-metrics">{row.format(item.value)}</span>
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <section className="space-y-3 sm:space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <h2 className="truncate text-lg font-semibold">{device.device_name}</h2>
          <MakeBadge make={device.make} showIcon />
          <span className="text-xs text-muted-foreground">
            {device.model || device.device_id} · {device.ip_address}
          </span>
        </div>
        <span className="text-xs text-muted-foreground">
          {summary.samples} samples · {GRANULARITY_LABEL[granularity]}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Hash}
          label="Average hashrate"
          value={formatHashrate(summary.avg_hashrate_ghs || 0)}
          subtitle={
            'min ' +
            formatHashrate(summary.min_hashrate_ghs || 0) +
            ' · max ' +
            formatHashrate(summary.max_hashrate_ghs || 0)
          }
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Flame}
          label="Chip temperature"
          value={formatNumber(summary.max_chip_temperature_c || 0, 0) + ' °C max'}
          subtitle={'avg ' + formatNumber(summary.avg_chip_temperature_c || 0, 0) + ' °C'}
          tone={
            (summary.max_chip_temperature_c || 0) >= 110
              ? 'danger'
              : (summary.max_chip_temperature_c || 0) >= 100
                ? 'warning'
                : 'default'
          }
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Zap}
          label="Energy"
          value={formatNumber(summary.energy_kwh || 0, 2) + ' kWh'}
          subtitle={
            money(summary.energy_cost, currency) +
            ' · avg ' +
            formatNumber(summary.avg_power_watts || 0, 0) +
            ' W'
          }
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Gauge}
          label="Efficiency"
          value={formatNumber(summary.avg_efficiency_j_per_th || 0, 1) + ' J/TH'}
          subtitle={'over ' + rangeLabel}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <MetricCard
          icon={Activity}
          label="Shares"
          value={formatNumber(summary.shares_accepted || 0, 0)}
          subtitle={formatNumber(summary.rejection_rate || 0, 2) + '% rejected'}
          tone={(summary.rejection_rate || 0) > 2 ? 'warning' : 'success'}
        />
        <MetricCard
          icon={Award}
          label="Best share"
          value={formatDifficulty(summary.best_difficulty || 0)}
          subtitle="in this period"
        />
        <MetricCard
          icon={Server}
          label="Data coverage"
          value={
            summary.availability_percent === null || summary.availability_percent === undefined
              ? '—'
              : formatNumber(summary.availability_percent, 1) + '%'
          }
          subtitle="of the period has samples"
        />
        <MetricCard
          icon={Coins}
          label="Pool worker"
          value={pool?.matched ? formatHashrate(pool.worker_hashrate_ghs || 0) : '—'}
          subtitle={
            pool
              ? pool.matched
                ? 'worker ' + pool.worker + ' · last share ' + relativeTime(pool.last_share_at)
                : 'no worker match' +
                  (pool.available_workers?.length
                    ? ' (pool knows: ' + pool.available_workers.join(', ') + ')'
                    : '')
              : 'no pool configured'
          }
        />
      </div>

      <div className="grid grid-cols-1 gap-3 sm:gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">Hashrate</CardTitle>
            <CardDescription className="text-xs">
              {GRANULARITY_LABEL[granularity]} · this device
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="h-[200px]">
              {series.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={series}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted/30" />
                    <XAxis
                      dataKey="t"
                      tickFormatter={timeAxis.tick}
                      minTickGap={timeAxis.minTickGap}
                      interval={timeAxis.interval}
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis className="text-xs" axisLine={false} tickLine={false} width={56} />
                    <Tooltip
                      content={tooltip([
                        {
                          key: 'hashrate_ghs',
                          label: 'Hashrate',
                          format: (value) => formatHashrate(value),
                        },
                      ])}
                    />
                    <Line
                      type="monotone"
                      dataKey="hashrate_ghs"
                      stroke="var(--chart-1)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  No samples in this period
                </p>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">Chip temperature &amp; power</CardTitle>
            <CardDescription className="text-xs">
              Chip solid · exhaust dashed · power right axis
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="h-[200px]">
              {series.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={series}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted/30" />
                    <XAxis
                      dataKey="t"
                      tickFormatter={timeAxis.tick}
                      minTickGap={timeAxis.minTickGap}
                      interval={timeAxis.interval}
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      yAxisId="left"
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                      width={48}
                      tickFormatter={(value) => formatNumber(value, 0) + '°'}
                    />
                    <YAxis
                      yAxisId="right"
                      orientation="right"
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                      width={48}
                      tickFormatter={(value) => formatNumber(value, 0) + 'W'}
                    />
                    <Tooltip
                      content={tooltip([
                        {
                          key: 'temperature_chip_c',
                          label: 'Chip',
                          format: (value) => formatNumber(value, 1) + ' °C',
                        },
                        {
                          key: 'temperature_c',
                          label: 'Exhaust',
                          format: (value) => formatNumber(value, 1) + ' °C',
                        },
                        {
                          key: 'power_watts',
                          label: 'Power',
                          format: (value) => formatNumber(value, 0) + ' W',
                        },
                      ])}
                    />
                    <Line
                      yAxisId="left"
                      type="monotone"
                      dataKey="temperature_chip_c"
                      stroke="var(--destructive)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      yAxisId="left"
                      type="monotone"
                      dataKey="temperature_c"
                      stroke="var(--chart-2)"
                      strokeWidth={1.5}
                      strokeDasharray="4 3"
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      yAxisId="right"
                      type="monotone"
                      dataKey="power_watts"
                      stroke="var(--chart-3)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  No samples in this period
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </section>
  )
}

function ComparisonTable({ rows, currency }) {
  if (!rows?.length) return null
  const columns = [
    { key: 'device_name', label: 'Device', render: (row) => row.device_name },
    {
      key: 'avg_hashrate_ghs',
      label: 'Hashrate',
      render: (row) => formatHashrate(row.avg_hashrate_ghs || 0),
    },
    {
      key: 'avg_power_watts',
      label: 'Power',
      render: (row) => formatNumber(row.avg_power_watts || 0, 0) + ' W',
    },
    {
      key: 'avg_efficiency_j_per_th',
      label: 'Efficiency',
      render: (row) => formatNumber(row.avg_efficiency_j_per_th || 0, 1) + ' J/TH',
    },
    {
      key: 'max_chip_temperature_c',
      label: 'Chip max',
      render: (row) => formatNumber(row.max_chip_temperature_c || 0, 0) + ' °C',
    },
    { key: 'energy_kwh', label: 'Energy', render: (row) => formatNumber(row.energy_kwh || 0, 2) + ' kWh' },
    { key: 'energy_cost', label: 'Cost', render: (row) => money(row.energy_cost, currency) },
    {
      key: 'rejection_rate',
      label: 'Rejected',
      render: (row) => formatNumber(row.rejection_rate || 0, 2) + '%',
    },
    {
      key: 'availability_percent',
      label: 'Coverage',
      render: (row) =>
        row.availability_percent === null || row.availability_percent === undefined
          ? '—'
          : formatNumber(row.availability_percent, 1) + '%',
    },
    {
      key: 'best_difficulty',
      label: 'Best share',
      render: (row) => formatDifficulty(row.best_difficulty || 0),
    },
  ]

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold">Device comparison</CardTitle>
        <CardDescription className="text-xs">
          Side by side for the selected period — no averaging across devices
        </CardDescription>
      </CardHeader>
      <CardContent className="px-2 sm:px-6">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
                {columns.map((column) => (
                  <th key={column.key} className="whitespace-nowrap px-2 py-2 font-medium">
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.device_id} className="border-b border-border/40 last:border-0">
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={
                        'whitespace-nowrap px-2 py-2 ' +
                        (column.key === 'device_name' ? 'font-medium' : 'tabular-metrics')
                      }
                    >
                      {column.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

export default function AnalyticsDashboard() {
  const { range } = useTimeRange()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)

  const fetchData = useCallback(async () => {
    try {
      setError(null)
      const response = await api.get('/api/analytics/devices/', {
        params: toAnalyticsParams(range),
      })
      setData(response.data)
      setUpdatedAt(new Date())
    } catch (err) {
      console.error('Error loading analytics:', err)
      setError(err)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- range identity covered by keys
  }, [range.key, range.from?.getTime?.(), range.to?.getTime?.(), range.hours, range.days])

  useEffect(() => {
    setLoading(true)
    fetchData()
    const timer = setInterval(fetchData, REFRESH_MS)
    return () => clearInterval(timer)
  }, [fetchData])

  if (loading && !data) return <DashboardSkeleton />

  if (error && !data) {
    return (
      <ErrorState
        title="Unable to load analytics"
        description="Could not load analytics data. Check that the API is running and try again."
        onRetry={() => {
          setLoading(true)
          fetchData()
        }}
        className="h-96"
      />
    )
  }

  if (!data?.devices?.length) {
    return (
      <EmptyState
        icon={Server}
        title="No devices yet"
        description="Add a miner to start collecting analytics."
        action={{ label: 'Add device', to: '/settings' }}
      />
    )
  }

  const currency = data.money?.currency || 'EUR'
  const btcPrice = data.money?.btc_price || 0

  return (
    <div className="space-y-6 sm:space-y-8">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs text-muted-foreground sm:text-sm">
            {range.label} · {GRANULARITY_LABEL[data.window?.granularity] || 'aggregated'} ·{' '}
            {btcPrice ? money(btcPrice, currency, 0) + ' per BTC' : 'no BTC price yet'}
          </p>
          <p className="mt-0.5 hidden text-[10px] text-muted-foreground/80 sm:block">
            {formatRangeWindow(range)}
            {data.money?.energy_rate
              ? ' · ' + formatNumber(data.money.energy_rate, 4) + ' ' + currency + '/kWh'
              : ' · no energy price set'}
          </p>
        </div>
        <DataFreshness updatedAt={updatedAt} live />
      </div>

      {data.accounts?.length > 0 && (
        <div className="space-y-3 sm:space-y-4">
          {data.accounts.map((account) => (
            <AccountCard
              key={account.pool_type + account.account}
              account={account}
              currency={currency}
              btcPrice={btcPrice}
            />
          ))}
        </div>
      )}

      {data.devices.map((entry) => (
        <DeviceBlock
          key={entry.device.device_id}
          entry={entry}
          currency={currency}
          granularity={data.window?.granularity}
          rangeHours={range.hours || data.window?.hours || 24}
          rangeLabel={range.label.toLowerCase()}
        />
      ))}

      <ComparisonTable rows={data.comparison} currency={currency} />
    </div>
  )
}
