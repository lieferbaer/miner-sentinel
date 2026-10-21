/**
 * Overview — total hashrate + device count at the top, then one full,
 * separate block of cards per device. No values averaged across devices.
 */
import {
  DashboardSkeleton,
  formatAxisHashrate,
  formatAxisPower,
  formatDifficulty,
  formatHashrate,
  formatNumber,
  formatShares,
} from '@/components/dashboard'
import MakeBadge from '@/components/devices/MakeBadge'
import ThrottleCard, { ThrottleStatusCard, useThrottle } from '@/components/devices/ThrottleCard'
import EmptyState from '@/components/feedback/EmptyState'
import ErrorState from '@/components/feedback/ErrorState'
import DataFreshness from '@/components/metrics/DataFreshness'
import MetricCard from '@/components/metrics/MetricCard'
import StatusIndicator from '@/components/status/StatusIndicator'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import FleetAlertsBar from '@/components/dashboard/FleetAlertsBar'
import api from '@/lib/api'
import { useLive } from '@/lib/LiveContext'
import InfoTip from '@/components/ui/info-tip'
import { useTimeRange } from '@/lib/TimeRangeContext'
import { formatRangeWindow, getChartTimeAxisConfig, toAnalyticsParams } from '@/lib/timeRange'
import {
  Activity,
  Award,
  Fan,
  Flame,
  Gauge,
  Hash,
  Server,
  SlidersHorizontal,
  Thermometer,
  Timer,
  Zap,
} from 'lucide-react'
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

const STALE_AFTER_MS = 10 * 60 * 1000
const TEMP_WARNING_C = 85
const CHIP_WARNING_C = 100
const CHIP_DANGER_C = 110
const SERIES_REFRESH_MS = 60000
const EFFECTIVE_CLOCK_INFO =
  'Effektiver Takt: der Durchschnittstakt, den der Miner selbst meldet (Freq, aktualisiert etwa alle 130 s). ' +
  'Er hängt davon ab, wie viele Chips auf welcher der 4 Takt-Stufen laufen. Drosselung und Lüfter-Automatik ' +
  'setzen die 4 Takt-Stufen und zeigen deren Mittel – deshalb weichen die Zahlen voneinander ab.'
const FAMILY_MODE_LABELS = { nano: ['Low', 'Medium', 'High'], q: ['Eco', 'Standard', 'Super'] }
function deviceFamily(device) {
  const text = [device.device_id, device.device_name, device.model].filter(Boolean).join(' ').toLowerCase()
  if (/nano/.test(text)) return 'nano'
  if (/a15/.test(text)) return 'a15'
  if (/avalonq|(^|\W)q(\W|$)/.test(text)) return 'q'
  return null
}
function workModeLabel(mode, family) {
  if (mode === null || mode === undefined) return '—'
  return (FAMILY_MODE_LABELS[family] || [])[mode] || 'Mode ' + mode
}
const TEMP_DANGER_C = 95

function deviceStatus(device) {
  if (!device.is_active) return 'unknown'
  const seenAt = device.last_seen_at ? new Date(device.last_seen_at).getTime() : 0
  if (!seenAt) return 'offline'
  return Date.now() - seenAt > STALE_AFTER_MS ? 'stale' : 'online'
}

function formatUptime(seconds) {
  const total = Number(seconds) || 0
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return days + 'd ' + hours + 'h'
  if (hours > 0) return hours + 'h ' + minutes + 'm'
  return minutes + 'm'
}

function tempTone(celsius, warning = TEMP_WARNING_C, danger = TEMP_DANGER_C) {
  if (celsius >= danger) return 'danger'
  if (celsius >= warning) return 'warning'
  return 'default'
}

function StatRow({ label, value }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate text-right font-medium tabular-metrics">{value}</span>
    </div>
  )
}

function ChartTooltip({ active, payload, label, timeAxis, rows }) {
  if (!active || !payload || payload.length === 0) return null
  return (
    <div className="rounded-lg border bg-background/95 p-3 shadow-lg backdrop-blur">
      <div className="mb-2 text-xs text-muted-foreground">{timeAxis.tooltip(label)}</div>
      {rows.map((row) => {
        const entry = payload.find((item) => item.dataKey === row.key)
        if (!entry) return null
        return (
          <div key={row.key} className="flex items-center justify-between gap-4 text-xs">
            <span className="text-muted-foreground">{row.label}</span>
            <span className="font-semibold tabular-metrics">{row.format(entry.value)}</span>
          </div>
        )
      })}
    </div>
  )
}

function DeviceSection({
  device,
  mining,
  hardware,
  system,
  miningSeries,
  hardwareSeries,
  rangeHours,
}) {
  const status = deviceStatus(device)
  const timeAxis = getChartTimeAxisConfig(rangeHours)
  const temperature = hardware?.temperature_c || 0
  const chipTemperature = hardware?.temperature_chip_c || 0
  const fanRpm = hardware?.fan_speed_rpm || 0
  const fanPercent = system?.fan_speed_percent
  const workMode = system?.work_mode
  const workLevel = system?.work_level
  const powerTarget = system?.power_target_watts
  const tempTarget = system?.temp_target
  const chipBoards =
    Array.isArray(system?.chip_temp_boards) && system.chip_temp_boards.length > 1
      ? system.chip_temp_boards.map((v) => (typeof v === 'number' && v > -50 && v < 200 ? v : null))
      : null
  const accepted = mining?.shares_accepted || 0
  const rejected = mining?.shares_rejected || 0
  const totalShares = accepted + rejected
  const acceptance = totalShares > 0 ? (accepted / totalShares) * 100 : 0
  const throttleEnabled = device.make === 'avalon' && status === 'online'
  const throttle = useThrottle(device.id, throttleEnabled)
  const family = deviceFamily(device)
  const isA15 = family === 'a15'
  const a15Mode = isA15
    ? workMode === 1
      ? { value: 'High Performance', sub: 'Mode 1' }
      : workMode === 2
        ? workLevel !== null && workLevel !== undefined
          ? { value: 'Power Mode ' + String(workLevel).padStart(2, '0'), sub: 'Work mode' }
          : { value: '—', sub: 'Stufe nicht lesbar' }
        : { value: workMode === null || workMode === undefined ? '—' : 'Mode ' + workMode, sub: 'Work mode' }
    : null

  return (
    <section className="space-y-3 sm:space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <h2 className="truncate text-lg font-semibold">
            {device.device_name || device.device_id}
          </h2>
          <MakeBadge make={device.make} showIcon />
          <span className="text-xs text-muted-foreground">
            {device.model || device.device_id} · {device.ip_address}
          </span>
        </div>
        <StatusIndicator status={status} showLabel size="md" />
      </div>

      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={SlidersHorizontal}
          label="Working mode"
          value={a15Mode ? a15Mode.value : workModeLabel(workMode, family)}
          subtitle={
            (a15Mode ? a15Mode.sub : workMode === null || workMode === undefined ? 'Unknown' : 'Mode ' + workMode) +
            (powerTarget ? ' · ' + formatNumber(powerTarget, 0) + ' W target' : '') +
            (workLevel && !a15Mode ? ' · level ' + workLevel : '')
          }
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Hash}
          label="Hashrate"
          value={formatHashrate(mining?.hashrate_ghs || 0)}
          subtitle={formatNumber(mining?.hashrate_ghs || 0, 2) + ' GH/s'}
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Flame}
          label="Chip temperature"
          value={
            chipBoards
              ? chipBoards.every((v) => v === null)
                ? '—'
                : chipBoards.map((v) => (v === null ? '—' : v)).join(' / ') + ' °C'
              : formatNumber(chipTemperature, 0) + ' °C'
          }
          subtitle={tempTarget ? 'Target ' + formatNumber(tempTarget, 0) + ' °C' : 'Hottest ASIC'}
          tone={tempTone(chipTemperature, CHIP_WARNING_C, CHIP_DANGER_C)}
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Thermometer}
          label="Exhaust temperature"
          value={formatNumber(temperature, 0) + ' °C'}
          subtitle={
            hardware?.temperature_board_c
              ? 'Hash board ' + formatNumber(hardware.temperature_board_c, 0) + ' °C'
              : 'Outlet air'
          }
          tone={tempTone(temperature)}
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Fan}
          label="Fan speed"
          value={formatNumber(fanRpm, 0) + ' rpm'}
          subtitle={
            fanPercent || fanPercent === 0 ? formatNumber(fanPercent, 0) + '% duty' : 'Measured'
          }
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Zap}
          label="Power"
          value={formatNumber(hardware?.power_watts || 0, 0) + ' W'}
          subtitle={
            formatNumber(((hardware?.power_watts || 0) / 1000) * 24, 1) + ' kWh per day'
          }
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Gauge}
          label="Efficiency"
          value={formatNumber(hardware?.efficiency_j_per_th || 0, 1) + ' J/TH'}
          subtitle={
            <span className="inline-flex items-center gap-1">
              effektiver Takt {formatNumber(hardware?.frequency_mhz || 0, 0)} MHz
              {device.make === 'avalon' && <InfoTip text={EFFECTIVE_CLOCK_INFO} />}
            </span>
          }
        />
        {throttleEnabled && <ThrottleStatusCard throttle={throttle} />}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <MetricCard
          icon={Award}
          label="Best share"
          value={formatDifficulty(mining?.best_difficulty || 0)}
          subtitle="This device"
        />
        <MetricCard
          icon={Activity}
          label="Shares"
          value={formatShares(accepted)}
          subtitle={formatNumber(acceptance, 1) + '% accepted · ' + rejected + ' rejected'}
          tone={acceptance >= 95 ? 'success' : 'warning'}
        />
        <MetricCard
          icon={Timer}
          label="Uptime"
          value={formatUptime(mining?.uptime_seconds)}
          subtitle="Since last restart"
        />
        <MetricCard
          icon={Zap}
          label="Voltage"
          value={formatNumber(hardware?.voltage || 0, 2) + ' V'}
          subtitle={device.make === 'avalon' ? 'PSU / core rail' : 'Core rail'}
        />
        {throttleEnabled && <ThrottleCard deviceId={device.id} throttle={throttle} ip={device.ip_address} />}

      </div>

      <div className="grid grid-cols-1 gap-3 sm:gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">Hashrate</CardTitle>
            <CardDescription className="text-xs">This device only</CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="h-[200px]">
              {miningSeries.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={miningSeries}>
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
                      tickFormatter={formatAxisHashrate}
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                      width={56}
                    />
                    <Tooltip
                      content={(props) => (
                        <ChartTooltip
                          {...props}
                          timeAxis={timeAxis}
                          rows={[
                            {
                              key: 'hashrate_ghs',
                              label: 'Hashrate',
                              format: (value) => formatHashrate(value),
                            },
                          ]}
                        />
                      )}
                    />
                    <Line
                      type="monotone"
                      dataKey="hashrate_ghs"
                      stroke="var(--chart-1)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  Not enough samples yet
                </p>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">Temperature &amp; Power</CardTitle>
            <CardDescription className="text-xs">
              Chip solid · exhaust dashed · this device
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="h-[200px]">
              {hardwareSeries.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={hardwareSeries}>
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
                      tickFormatter={(value) => value + '°C'}
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                      width={48}
                    />
                    <YAxis
                      yAxisId="right"
                      orientation="right"
                      tickFormatter={formatAxisPower}
                      className="text-xs"
                      axisLine={false}
                      tickLine={false}
                      width={48}
                    />
                    <Tooltip
                      content={(props) => (
                        <ChartTooltip
                          {...props}
                          timeAxis={timeAxis}
                          rows={[
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
                          ]}
                        />
                      )}
                    />
                    <Line
                      yAxisId="left"
                      type="monotone"
                      dataKey="temperature_chip_c"
                      stroke="var(--destructive)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
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
                    />
                    <Line
                      yAxisId="right"
                      type="monotone"
                      dataKey="power_watts"
                      stroke="var(--chart-3)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  Not enough samples yet
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">Pool</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <StatRow label="URL" value={mining?.pool_url || '—'} />
            <StatRow label="Worker" value={mining?.pool_user || '—'} />
            <StatRow label="Accepted" value={formatShares(accepted)} />
            <StatRow label="Rejected" value={formatNumber(rejected, 0)} />
            <StatRow label="Blocks found" value={formatNumber(mining?.blocks_found || 0, 0)} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">Device</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <StatRow label="Device ID" value={device.device_id} />
            <StatRow label="Model" value={device.model || '—'} />
            <StatRow label="Address" value={device.ip_address + ':' + (device.port || '—')} />
            <StatRow label="Protocol" value={device.protocol} />
            <StatRow
              label="Last sample"
              value={
                device.last_seen_at ? new Date(device.last_seen_at).toLocaleString() : 'never'
              }
            />
          </CardContent>
        </Card>
      </div>

      {device.error_message ? (
        <p className="text-sm text-status-critical-fg">{device.error_message}</p>
      ) : null}
    </section>
  )
}

export default function OverviewDashboard() {
  const { range } = useTimeRange()
  const [devices, setDevices] = useState([])
  const [mining, setMining] = useState({})
  const [hardware, setHardware] = useState({})
  const [system, setSystem] = useState({})
  const [series, setSeries] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)

  // live values: the shared poll of the whole UI (LiveContext, every 5 s) - same values on every page
  const live = useLive()
  useEffect(() => {
    const d = live.data
    if (!d) {
      if (live.error) {
        setError(live.error)
        setLoading(false)
      }
      return
    }
    const deviceList = Array.isArray(d.devices) ? d.devices : []
    const byDevice = (rows) => {
      const map = {}
      ;(Array.isArray(rows) ? rows : []).forEach((row) => {
        const key = row.device_id_str || row.device_id
        if (key) map[key] = row
      })
      return map
    }
    setError(null)
    setDevices(deviceList)
    setMining(byDevice(d.mining))
    setHardware(byDevice(d.hardware))
    setSystem(Object.fromEntries(deviceList.map((dev) => [dev.device_id, (d.system || {})[dev.device_id] || null])))
    setUpdatedAt(live.loadedAt ? new Date(live.loadedAt) : new Date())
    setLoading(false)
  }, [live.data, live.error, live.loadedAt])

  const fetchSeries = useCallback(
    async (deviceList) => {
      const list = deviceList && deviceList.length ? deviceList : devices
      if (!list.length) return
      const params = toAnalyticsParams(range)
      const entries = await Promise.all(
        list.map(async (device) => {
          const load = async (path, mapper) => {
            try {
              const response = await api.get(path, {
                params: { ...params, device_id: device.device_id },
              })
              const rows = response.data?.results || response.data || []
              return rows.map(mapper).sort((a, b) => new Date(a.t) - new Date(b.t))
            } catch {
              return []
            }
          }
          const [miningRows, hardwareRows] = await Promise.all([
            load('/api/mining/', (row) => ({
              t: row.recorded_at,
              hashrate_ghs: row.hashrate_ghs || 0,
            })),
            load('/api/hardware/', (row) => ({
              t: row.recorded_at,
              temperature_c: row.temperature_c || 0,
              temperature_chip_c: row.temperature_chip_c || 0,
              power_watts: row.power_watts || 0,
            })),
          ])
          return [device.device_id, { mining: miningRows, hardware: hardwareRows }]
        }),
      )
      setSeries(Object.fromEntries(entries))
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- range identity is covered by the keys below
    [devices, range.key, range.from?.getTime?.(), range.to?.getTime?.(), range.hours, range.days],
  )

  // charts: own requests (large, change slowly); reloaded when the device list or the range changes
  const deviceKey = devices.map((d) => d.device_id).join(',')
  useEffect(() => {
    if (!deviceKey) return undefined
    fetchSeries(devices)
    const seriesTimer = setInterval(() => fetchSeries(), SERIES_REFRESH_MS)
    return () => clearInterval(seriesTimer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- restart when the devices or the range change
  }, [deviceKey, range.key, range.from?.getTime?.(), range.to?.getTime?.(), range.hours, range.days])

  if (loading && devices.length === 0) {
    return <DashboardSkeleton />
  }

  if (error && devices.length === 0) {
    return (
      <ErrorState
        title="Unable to load overview"
        description="Could not load device data. Check that the API is running and try again."
        onRetry={() => {
          setLoading(true)
          live.refresh()
        }}
        className="h-96"
      />
    )
  }

  if (devices.length === 0) {
    return (
      <EmptyState
        icon={Server}
        title="No devices yet"
        description="Add a Bitaxe, Avalon, or other supported miner to start monitoring."
        action={{ label: 'Add device', to: '/settings' }}
      />
    )
  }

  const activeDevices = devices.filter((device) => device.is_active)
  const onlineDevices = activeDevices.filter((device) => deviceStatus(device) === 'online')
  const totalHashrate = activeDevices.reduce(
    (sum, device) => sum + (mining[device.device_id]?.hashrate_ghs || 0),
    0,
  )
  const totalPower = activeDevices.reduce(
    (sum, device) => sum + (hardware[device.device_id]?.power_watts || 0),
    0,
  )

  return (
    <div className="space-y-6 sm:space-y-8">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs text-muted-foreground sm:text-sm">
            Live values every 10 s · charts for{' '}
            <span className="font-medium text-foreground/80">{range.label.toLowerCase()}</span>
          </p>
          <p className="mt-0.5 hidden text-[10px] text-muted-foreground/80 sm:block">
            {formatRangeWindow(range)}
          </p>
        </div>
        <DataFreshness updatedAt={updatedAt} live />
      </div>

      <FleetAlertsBar
        offlineCount={activeDevices.filter((device) => deviceStatus(device) === 'offline').length}
      />

      <div className="grid grid-cols-1 gap-3 sm:gap-4 xl:grid-cols-2">
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Hash}
          label="Total hashrate"
          value={formatHashrate(totalHashrate)}
          subtitle={'Sum of all devices · ' + formatNumber(totalPower, 0) + ' W total'}
        />
        <MetricCard
          variant="hero"
          layout="icon-left"
          icon={Server}
          label="Devices"
          value={onlineDevices.length + ' / ' + activeDevices.length + ' online'}
          subtitle={
            devices.length + ' registered' + (devices.length > activeDevices.length ? ' · some disabled' : '')
          }
          tone={onlineDevices.length === activeDevices.length ? 'success' : 'warning'}
        />
      </div>

      {devices.map((device) => (
        <DeviceSection
          key={device.device_id}
          device={device}
          mining={mining[device.device_id]}
          hardware={hardware[device.device_id]}
          system={system[device.device_id]}
          miningSeries={series[device.device_id]?.mining || []}
          hardwareSeries={series[device.device_id]?.hardware || []}
          rangeHours={range.hours || 24}
        />
      ))}
    </div>
  )
}
