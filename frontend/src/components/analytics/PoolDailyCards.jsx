/**
 * Balance und Pool-Hashrate je Tag (v2.2.9, Nutzer 08.11.): one card per pool account, always the last 160 days
 * (independent of the page's time range). Bars: pool 24-h hashrate at the end of the day (left axis), line: account
 * balance at the end of the day (right axis).
 */
import { formatHashrate, formatNumber } from '@/components/dashboard'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { useEffect, useState } from 'react'
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const REFRESH_MS = 5 * 60 * 1000
const TITLE = 'Balance und Pool-Hashrate je Tag (160 Tage)'

// 'YYYY-MM-DD' → 'dd.MM.' / 'dd.MM.yyyy' (string based: no time zone shift)
function dayTick(day) {
  const [, month, date] = String(day).split('-')
  return date + '.' + month + '.'
}

function dayLabel(day) {
  const [year, month, date] = String(day).split('-')
  return date + '.' + month + '.' + year
}

function DailyTooltip({ active, payload, label }) {
  if (!active || !payload || payload.length === 0) return null
  const point = payload[0]?.payload || {}
  return (
    <div className="rounded-lg border bg-background/95 p-3 shadow-lg backdrop-blur">
      <div className="mb-2 text-xs text-muted-foreground">{dayLabel(label)}</div>
      <div className="flex items-center justify-between gap-4 text-sm">
        <span className="text-muted-foreground">Pool-Hashrate (24 h)</span>
        <span className="font-semibold tabular-metrics">
          {point.hashrate_ghs === null || point.hashrate_ghs === undefined
            ? '—'
            : formatHashrate(point.hashrate_ghs)}
        </span>
      </div>
      <div className="flex items-center justify-between gap-4 text-sm">
        <span className="text-muted-foreground">Balance</span>
        <span className="font-semibold tabular-metrics">
          {point.balance_btc === null || point.balance_btc === undefined
            ? '—'
            : formatNumber(point.balance_btc, 8) + ' BTC'}
        </span>
      </div>
    </div>
  )
}

function EmptyText() {
  return (
    <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
      Keine Daten in den letzten 160 Tagen
    </p>
  )
}

function PoolDailyCard({ account }) {
  const series = (account?.series || []).map((point) => ({
    ...point,
    hashrate_ths:
      point.hashrate_ghs === null || point.hashrate_ghs === undefined ? null : point.hashrate_ghs / 1000,
  }))
  const hasData = series.some(
    (point) =>
      (point.balance_btc !== null && point.balance_btc !== undefined) ||
      (point.hashrate_ghs !== null && point.hashrate_ghs !== undefined)
  )
  const maxBalance = Math.max(0, ...series.map((point) => point.balance_btc || 0))
  const btcDecimals = maxBalance >= 1 ? 3 : maxBalance >= 0.01 ? 4 : 6

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold">{TITLE}</CardTitle>
        <CardDescription className="text-xs">
          {account ? account.account + ' · ' + account.pool_type + ' · ' : ''}
          Balken: Pool-Hashrate (24 h) · Linie: Balance (rechts) · Wert am Tagesende
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="h-[240px]">
          {hasData ? (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={series}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted/30" />
                <XAxis
                  dataKey="day"
                  tickFormatter={dayTick}
                  minTickGap={16}
                  interval="preserveStartEnd"
                  className="text-xs"
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  yAxisId="left"
                  className="text-xs"
                  axisLine={false}
                  tickLine={false}
                  width={64}
                  tickFormatter={(value) =>
                    formatNumber(value, value !== 0 && Math.abs(value) < 10 ? 1 : 0) + ' TH/s'
                  }
                />
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  className="text-xs"
                  axisLine={false}
                  tickLine={false}
                  width={64}
                  tickFormatter={(value) => formatNumber(value, btcDecimals)}
                />
                <Tooltip content={<DailyTooltip />} />
                <Bar
                  yAxisId="left"
                  dataKey="hashrate_ths"
                  fill="var(--chart-1)"
                  isAnimationActive={false}
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="balance_btc"
                  stroke="var(--chart-3)"
                  strokeWidth={2}
                  dot={false}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <EmptyText />
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export default function PoolDailyCards() {
  const [data, setData] = useState(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const response = await api.get('/api/analytics/pool-daily/')
        if (alive) setData(response.data)
      } catch (err) {
        console.error('Error loading pool daily values:', err)
      }
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  if (!data) return null

  if (!data.accounts?.length) return <PoolDailyCard account={null} />

  return (
    <div className="space-y-3 sm:space-y-4">
      {data.accounts.map((account) => (
        <PoolDailyCard key={account.pool_type + account.account} account={account} />
      ))}
    </div>
  )
}
