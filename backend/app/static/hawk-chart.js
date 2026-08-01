(function bootstrapHawkEyeChart() {
  'use strict';

  const state = {
    chart: null,
    candles: new Map(),
    contract: null,
    annotationsVisible: true,
    autoFollow: true,
    zoom: { start: 52, end: 100 },
    resizeObserver: null,
    controlsBound: false,
    futureBars: 32,
  };

  const element = (id) => document.getElementById(id);
  const palette = {
    cyan: '#51dcff',
    cyanSoft: 'rgba(81, 220, 255, 0.45)',
    green: '#46e7aa',
    red: '#ff6678',
    amber: '#f4bd55',
    purple: '#8492ff',
    grid: 'rgba(122, 155, 184, 0.075)',
    text: '#8ca2b8',
  };

  function finite(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function price(value) {
    const number = finite(value);
    if (number === null) return '—';
    if (Math.abs(number) >= 1000) return number.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (Math.abs(number) >= 1) return number.toLocaleString(undefined, { maximumFractionDigits: 5 });
    return number.toLocaleString(undefined, { maximumSignificantDigits: 7 });
  }

  function compactVolume(value) {
    const number = finite(value) || 0;
    return Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 2 }).format(number);
  }

  function signedUsd(value) {
    const number = finite(value);
    if (number === null) return 'Δ unavailable';
    const formatted = Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 }).format(Math.abs(number));
    return `Δ ${number >= 0 ? '+' : '−'}${formatted}`;
  }

  function timeLabel(value, includeDate = false) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp)) return String(value || '');
    const date = new Date(timestamp);
    return new Intl.DateTimeFormat(undefined, includeDate
      ? { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' }
    ).format(date);
  }

  function candleIntervalMs(candles, timeframe) {
    if (candles.length > 1) {
      const observed = candles[candles.length - 1].open_time - candles[candles.length - 2].open_time;
      if (Number.isFinite(observed) && observed > 0) return observed;
    }
    const units = { m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };
    const match = String(timeframe || '').toLowerCase().match(/^(\d+)([mhdw])$/);
    return match ? Number(match[1]) * units[match[2]] : 900_000;
  }

  function toneForDirection(direction) {
    if (String(direction).toUpperCase() === 'BULLISH') return palette.green;
    if (String(direction).toUpperCase() === 'BEARISH') return palette.red;
    return palette.cyan;
  }

  function eventShortLabel(event) {
    const type = event.type === 'LIQUIDITY_SWEEP' ? 'SWEEP' : event.type || 'EVENT';
    const arrow = event.direction === 'BULLISH' ? '▲' : event.direction === 'BEARISH' ? '▼' : '•';
    return `${type} ${arrow}`;
  }

  function isHiddenTerminalEvent(event) {
    return ['INVALIDATED', 'MISSED'].includes(String(event?.state || '').toUpperCase());
  }

  function mergeCandles(contract) {
    const rows = Array.isArray(contract.candles) ? contract.candles : [];
    if (contract.mode === 'snapshot') state.candles.clear();
    rows.forEach((row) => {
      const key = Number(row?.open_time);
      if (!Number.isFinite(key)) return;
      const normalized = {
        open_time: key,
        close_time: finite(row.close_time),
        open: finite(row.open),
        high: finite(row.high),
        low: finite(row.low),
        close: finite(row.close),
        volume: finite(row.volume) || 0,
        taker_buy_base_volume: finite(row.taker_buy_base_volume),
      };
      if ([normalized.open, normalized.high, normalized.low, normalized.close].some((item) => item === null)) return;
      state.candles.set(key, normalized);
    });
    const ordered = [...state.candles.keys()].sort((a, b) => a - b);
    ordered.slice(0, Math.max(0, ordered.length - 200)).forEach((key) => state.candles.delete(key));
  }

  function ensureChart() {
    if (state.chart) return state.chart;
    const root = element('hawk-eye-chart');
    if (!root || !window.echarts) {
      const emptyCopy = element('hawk-chart-empty');
      if (emptyCopy && !window.echarts) {
        emptyCopy.querySelector('strong').textContent = 'Chart engine unavailable';
        emptyCopy.querySelector('p').textContent = 'The chart library could not load. Check the browser network policy and retry.';
      }
      return null;
    }
    state.chart = window.echarts.init(root, null, { renderer: 'canvas', useDirtyRect: true });
    state.chart.on('datazoom', (event) => {
      const zoom = event.batch?.[0] || event;
      if (finite(zoom.start) !== null && finite(zoom.end) !== null) {
        const userChangedViewport = (
          Math.abs(Number(zoom.start) - state.zoom.start) > 0.25
          || Math.abs(Number(zoom.end) - state.zoom.end) > 0.25
        );
        state.zoom = { start: Number(zoom.start), end: Number(zoom.end) };
        if (state.autoFollow && userChangedViewport) {
          state.autoFollow = false;
          updateControlState();
        }
      }
    });
    state.chart.on('click', (params) => {
      const detail = params?.data?.hawkDetail;
      if (!detail) return;
      const stateCopy = detail.state ? ` · ${String(detail.state).replace(/_/g, ' ')}` : '';
      window.showAppToast?.(`${eventShortLabel(detail)}${stateCopy}: ${detail.reason || 'Completed-candle event.'}`, 'info', 7000);
    });
    if (window.ResizeObserver) {
      state.resizeObserver = new ResizeObserver(() => state.chart?.resize());
      state.resizeObserver.observe(root);
    } else {
      window.addEventListener('resize', () => state.chart?.resize());
    }
    return state.chart;
  }

  function eventMarkers(annotations) {
    if (!state.annotationsVisible) return [];
    const combined = [
      ...(annotations.structure_events || []).filter((event) => !isHiddenTerminalEvent(event)).slice(0, 6),
      ...(annotations.liquidity_events || []).filter((event) => !isHiddenTerminalEvent(event)).slice(0, 4),
    ];
    const selectedId = annotations.selected_event?.id;
    const markers = combined.map((event) => {
      const color = toneForDirection(event.direction);
      const isSelected = event.id === selectedId;
      return {
        name: eventShortLabel(event),
        coord: [String(event.time), finite(event.price) ?? finite(event.level)],
        value: eventShortLabel(event),
        symbol: event.type === 'LIQUIDITY_SWEEP' ? 'diamond' : 'pin',
        symbolSize: isSelected ? 48 : 34,
        symbolRotate: event.direction === 'BEARISH' && event.type !== 'LIQUIDITY_SWEEP' ? 180 : 0,
        itemStyle: { color, borderColor: '#07111d', borderWidth: 1.5, shadowBlur: isSelected ? 14 : 5, shadowColor: color },
        label: {
          show: isSelected,
          formatter: eventShortLabel(event),
          color: '#eefaff',
          fontSize: 9,
          fontWeight: 800,
          backgroundColor: 'rgba(5, 12, 22, 0.88)',
          borderColor: color,
          borderWidth: 1,
          borderRadius: 5,
          padding: [4, 6],
          distance: 7,
        },
        hawkDetail: event,
      };
    }).filter((item) => item.coord[1] !== null);
    const selected = annotations.selected_event || {};
    if (!isHiddenTerminalEvent(selected) && selected.retest_time && finite(selected.level) !== null) {
      markers.push({
        name: 'COMPLETED RETEST',
        coord: [String(selected.retest_time), Number(selected.level)],
        value: 'RETEST',
        symbol: 'diamond',
        symbolSize: 34,
        itemStyle: { color: palette.amber, borderColor: '#07111d', borderWidth: 1.5, shadowBlur: 9, shadowColor: palette.amber },
        label: {
          show: true, formatter: 'RETEST', color: palette.amber, fontSize: 9, fontWeight: 800,
          backgroundColor: 'rgba(5, 12, 22, 0.88)', borderColor: palette.amber, borderWidth: 1, borderRadius: 5, padding: [4, 6], distance: 6,
        },
        hawkDetail: { ...selected, type: 'RETEST', reason: 'A completed candle returned to the selected event level.' },
      });
    }
    const campaign = annotations.campaign || {};
    if (!isHiddenTerminalEvent(selected) && campaign.origin_time && finite(campaign.origin_price) !== null) {
      markers.push({
        name: 'CAUSAL ORIGIN', coord: [String(campaign.origin_time), Number(campaign.origin_price)], value: 'ORIGIN',
        symbol: 'circle', symbolSize: 18,
        itemStyle: { color: '#07111d', borderColor: palette.amber, borderWidth: 2.5, shadowBlur: 10, shadowColor: palette.amber },
        label: { show: true, formatter: 'ORIGIN', position: 'top', color: palette.amber, fontSize: 9, fontWeight: 800 },
      });
    }
    return markers;
  }

  function lineLevel(name, value, color, type = 'dashed', width = 1, position = 'insideEndTop') {
    const level = finite(value);
    if (level === null) return null;
    return {
      name,
      yAxis: level,
      lineStyle: { color, type, width, opacity: 0.82 },
      label: {
        show: state.annotationsVisible,
        formatter: `${name}  ${price(level)}`,
        position,
        color,
        fontSize: 9,
        fontWeight: 700,
        backgroundColor: 'rgba(4, 10, 18, 0.86)',
        borderRadius: 4,
        padding: [3, 5],
      },
    };
  }

  function annotationLines(annotations, latestClose) {
    const lines = [];
    lines.push(lineLevel('LIVE', latestClose, palette.cyanSoft, 'dotted', 1));
    if (!state.annotationsVisible) return lines.filter(Boolean);
    const selected = annotations.selected_event || {};
    const selectedIsHidden = isHiddenTerminalEvent(selected);
    if (!selectedIsHidden && selected.level !== undefined) {
      lines.push(lineLevel(`${selected.type || 'STRUCTURE'} · ${selected.state || 'DEVELOPING'}`, selected.level, toneForDirection(selected.direction), 'dashed', 1.4));
    }
    const campaign = annotations.campaign || {};
    if (!selectedIsHidden && campaign.origin_price !== undefined) {
      lines.push(lineLevel('CAUSAL ORIGIN', campaign.origin_price, palette.amber, 'dotted', 1.2));
    }
    (annotations.liquidity_levels || []).slice(0, 5).forEach((pool) => {
      lines.push(lineLevel(String(pool.kind || 'LIQUIDITY').replace(/_/g, ' '), pool.price, palette.purple, 'dotted', 1));
    });
    (annotations.execution_levels || []).forEach((level) => {
      const isStop = level.kind === 'STOP';
      lines.push(lineLevel(level.kind, level.price, isStop ? palette.red : palette.green, isStop ? 'dashed' : 'solid', 1.15));
    });
    return lines.filter(Boolean);
  }

  function entryAreas(annotations, times) {
    if (!state.annotationsVisible || !times.length) return [];
    if (isHiddenTerminalEvent(annotations.selected_event)) return [];
    const zone = annotations.entry_zone;
    if (!zone || finite(zone.low) === null || finite(zone.high) === null) return [];
    const selectedTime = String(annotations.selected_event?.time || times[Math.max(0, times.length - 35)]);
    const color = zone.execution_permitted ? palette.green : palette.amber;
    return [[
      {
        name: zone.execution_permitted ? 'CONFIRMED ENTRY ZONE' : 'CONDITIONAL WATCH ZONE',
        xAxis: times.includes(selectedTime) ? selectedTime : times[Math.max(0, times.length - 35)],
        yAxis: zone.low,
        itemStyle: { color: zone.execution_permitted ? 'rgba(70, 231, 170, 0.10)' : 'rgba(244, 189, 85, 0.085)', borderColor: color, borderWidth: 1 },
        label: { show: true, color, fontSize: 9, fontWeight: 800, position: 'insideTopLeft' },
      },
      { xAxis: times[times.length - 1], yAxis: zone.high },
    ]];
  }

  function tooltipFormatter(params) {
    const rows = Array.isArray(params) ? params : [params];
    const candle = rows.find((item) => item.seriesName === 'Price');
    if (!candle) return '';
    const time = candle.axisValue;
    const value = candle.value || [];
    if (!Array.isArray(value) || finite(value[0]) === null) return '';
    const volume = rows.find((item) => item.seriesName === 'Volume')?.value;
    const cvdPoint = rows.find((item) => item.seriesName === 'Aggressor CVD');
    const candleDelta = finite(cvdPoint?.data?.candleDelta);
    const color = Number(value[1]) >= Number(value[0]) ? palette.green : palette.red;
    return `
      <div style="min-width:190px;font:11px Inter,sans-serif;color:#8096ab">
        <div style="color:#eaf7ff;font-weight:800;margin-bottom:7px">${timeLabel(time, true)}</div>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:5px 12px">
          <span>Open <b style="color:#d9e9f7">${price(value[0])}</b></span>
          <span>High <b style="color:#d9e9f7">${price(value[3])}</b></span>
          <span>Low <b style="color:#d9e9f7">${price(value[2])}</b></span>
          <span>Close <b style="color:${color}">${price(value[1])}</b></span>
        </div>
        <div style="margin-top:6px;border-top:1px solid rgba(255,255,255,.08);padding-top:5px">Volume <b style="color:#d9e9f7">${compactVolume(volume)}</b>${candleDelta === null ? '' : ` · Taker Δ <b style="color:${candleDelta >= 0 ? palette.green : palette.red}">${candleDelta >= 0 ? '+' : ''}${compactVolume(candleDelta)}</b>`}</div>
      </div>`;
  }

  function render() {
    const chart = ensureChart();
    if (!chart || !state.contract) return;
    const candles = [...state.candles.values()].sort((a, b) => a.open_time - b.open_time);
    if (!candles.length) return;
    const realTimes = candles.map((row) => String(row.open_time));
    const annotations = state.contract.annotations || {};
    const latest = candles[candles.length - 1];
    const interval = candleIntervalMs(candles, state.contract.timeframe);
    const futureTimes = Array.from(
      { length: state.futureBars },
      (_, index) => String(latest.open_time + interval * (index + 1)),
    );
    const times = [...realTimes, ...futureTimes];
    const futurePadding = Array(state.futureBars).fill('-');
    let runningCvd = 0;
    const cvd = candles.map((row) => {
      const takerBuy = finite(row.taker_buy_base_volume);
      const delta = takerBuy === null ? 0 : takerBuy - Math.max(row.volume - takerBuy, 0);
      runningCvd += delta;
      return { value: runningCvd, candleDelta: takerBuy === null ? null : delta };
    });
    if (state.autoFollow) {
      const firstVisibleIndex = Math.max(0, candles.length - 88);
      state.zoom = {
        start: times.length > 1 ? firstVisibleIndex / (times.length - 1) * 100 : 0,
        end: 100,
      };
    }

    const option = {
      animation: false,
      backgroundColor: 'transparent',
      axisPointer: { link: [{ xAxisIndex: [0, 1] }], label: { backgroundColor: '#172537' } },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: 'rgba(82, 220, 255, .28)' } },
        backgroundColor: 'rgba(5, 11, 20, .96)',
        borderColor: 'rgba(82, 220, 255, .18)',
        borderWidth: 1,
        padding: 10,
        formatter: tooltipFormatter,
      },
      grid: [
        { left: 14, right: 82, top: 18, height: '68%', containLabel: false },
        { left: 14, right: 82, top: '75%', height: '13%', containLabel: false },
      ],
      xAxis: [
        {
          type: 'category', data: times, boundaryGap: true, axisLine: { lineStyle: { color: palette.grid } },
          axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: true, lineStyle: { color: palette.grid } },
          min: 0, max: times.length - 1,
        },
        {
          type: 'category', gridIndex: 1, data: times, boundaryGap: true,
          axisLine: { lineStyle: { color: 'rgba(120, 151, 180, .16)' } }, axisTick: { show: false },
          axisLabel: { color: palette.text, fontSize: 9, margin: 10, formatter: (value) => timeLabel(value) },
          splitLine: { show: false }, min: 0, max: times.length - 1,
        },
      ],
      yAxis: [
        {
          scale: true, position: 'right', splitNumber: 7,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: palette.text, fontSize: 9, margin: 9, formatter: price },
          splitLine: { show: true, lineStyle: { color: palette.grid } },
        },
        {
          scale: true, gridIndex: 1, position: 'right', axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { show: false }, splitLine: { show: false },
        },
        {
          scale: true, gridIndex: 1, position: 'left', axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { show: false }, splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: state.zoom.start, end: state.zoom.end, zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false },
        {
          type: 'slider', xAxisIndex: [0, 1], start: state.zoom.start, end: state.zoom.end,
          height: 15, bottom: 7, borderColor: 'rgba(83, 127, 160, .12)', backgroundColor: 'rgba(4, 10, 19, .72)',
          fillerColor: 'rgba(56, 191, 235, .10)', handleStyle: { color: '#4bdcff', borderColor: '#081421' },
          dataBackground: { lineStyle: { color: '#274861' }, areaStyle: { color: '#102638' } },
          selectedDataBackground: { lineStyle: { color: '#3fcce9' }, areaStyle: { color: '#12364a' } },
          textStyle: { color: '#60778c', fontSize: 8 }, showDetail: false,
        },
      ],
      series: [
        {
          name: 'Price', type: 'candlestick', data: [
            ...candles.map((row) => [row.open, row.close, row.low, row.high]),
            ...futurePadding,
          ],
          itemStyle: { color: '#20d5b2', color0: '#f05261', borderColor: '#20d5b2', borderColor0: '#f05261' },
          emphasis: { itemStyle: { borderWidth: 1.4 } },
          markPoint: { silent: false, data: eventMarkers(annotations), tooltip: { show: false } },
          markLine: { silent: true, symbol: ['none', 'none'], precision: 8, data: annotationLines(annotations, latest.close) },
          markArea: { silent: true, data: entryAreas(annotations, realTimes) },
        },
        {
          name: 'Volume', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: [
            ...candles.map((row) => ({ value: row.volume, itemStyle: { color: row.close >= row.open ? 'rgba(32, 213, 178, .48)' : 'rgba(240, 82, 97, .46)' } })),
            ...futurePadding,
          ],
          barMaxWidth: 8,
        },
        {
          name: 'Aggressor CVD', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: [...cvd, ...futurePadding],
          showSymbol: false, smooth: false, silent: true,
          lineStyle: { color: 'rgba(82, 213, 255, .88)', width: 1.2 },
          areaStyle: { color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(82, 213, 255, .18)' },
            { offset: 1, color: 'rgba(82, 213, 255, 0)' },
          ]) },
        },
      ],
    };
    chart.setOption(option, { notMerge: true, lazyUpdate: true });
  }

  function displayAction(decision) {
    if (decision.execution_permitted) return 'QUALIFIED MANUAL REVIEW';
    const timing = String(decision.entry_timing || '').toUpperCase();
    const storyState = String(decision.story_state || '').toUpperCase();
    if (timing === 'DO_NOT_CHASE' || ['EXTENDED_DO_NOT_CHASE', 'LATE_STRUCTURE_DO_NOT_CHASE', 'MISSED'].includes(storyState)) return 'DO NOT CHASE';
    if (timing === 'WAIT_FOR_PULLBACK' || storyState === 'PULLBACK_REQUIRED') return 'PULLBACK REQUIRED';
    if (storyState === 'RETESTING' && !decision.live_confirmation_passed) return 'LIVE PROOF REQUIRED';
    return String(decision.action || 'MONITORING').replace(/_/g, ' ');
  }

  function updateMeta(contract) {
    const decision = contract.decision || {};
    const flow = decision.flow || {};
    const setText = (id, text) => { const node = element(id); if (node) node.textContent = text; };
    setText('hawk-chart-symbol', contract.symbol || '—');
    setText('hawk-chart-timeframe', contract.timeframe || '—');
    setText('hawk-chart-story-state', String(decision.story_state || 'NO ACTIVE EVENT').replace(/_/g, ' '));
    const campaign = contract.annotations?.campaign || {};
    const distance = finite(campaign.distance_atr);
    setText('hawk-chart-maturity', `${String(decision.campaign_maturity || 'UNKNOWN').replace(/_/g, ' ')}${distance === null ? '' : ` · ${distance.toFixed(2)} ATR`}`);
    setText('hawk-chart-flow', flow.available ? `${String(flow.bias).replace(/_/g, ' ')} · ${signedUsd(flow.net_delta_usd)} · ${flow.confidence}` : 'UNAVAILABLE');
    setText('hawk-chart-action', displayAction(decision));
    setText('hawk-chart-reason', decision.live_confirmation_reason || decision.reason || 'Historical evidence explains the move; live evidence earns the entry.');
    const badge = element('hawk-chart-live-badge');
    if (badge) { badge.className = 'hawk-chart-live-badge live'; badge.innerHTML = '<i></i>LIVE'; }
  }

  function updateControlState() {
    const annotations = element('hawk-chart-annotations');
    const follow = element('hawk-chart-follow');
    annotations?.classList.toggle('is-active', state.annotationsVisible);
    annotations?.setAttribute('aria-pressed', String(state.annotationsVisible));
    follow?.classList.toggle('is-active', state.autoFollow);
    follow?.setAttribute('aria-pressed', String(state.autoFollow));
  }

  function bindControls() {
    if (state.controlsBound) return;
    state.controlsBound = true;
    element('hawk-chart-annotations')?.addEventListener('click', () => {
      state.annotationsVisible = !state.annotationsVisible;
      updateControlState();
      render();
    });
    element('hawk-chart-follow')?.addEventListener('click', () => {
      state.autoFollow = !state.autoFollow;
      updateControlState();
      render();
    });
    element('hawk-chart-reset')?.addEventListener('click', () => {
      state.autoFollow = true;
      state.zoom = { start: 52, end: 100 };
      updateControlState();
      render();
    });
  }

  function consume(contract) {
    if (!contract || contract.schema_version !== 'hawk_eye_chart.v1') return;
    bindControls();
    state.contract = contract;
    mergeCandles(contract);
    element('hawk-chart-empty')?.classList.add('is-hidden');
    updateMeta(contract);
    render();
  }

  function reset() {
    state.candles.clear();
    state.contract = null;
    state.chart?.clear();
    element('hawk-chart-empty')?.classList.remove('is-hidden');
    const badge = element('hawk-chart-live-badge');
    if (badge) { badge.className = 'hawk-chart-live-badge offline'; badge.innerHTML = '<i></i>OFFLINE'; }
    const setText = (id, text) => { const node = element(id); if (node) node.textContent = text; };
    setText('hawk-chart-story-state', 'AWAITING STREAM');
    setText('hawk-chart-maturity', 'UNKNOWN');
    setText('hawk-chart-flow', 'UNAVAILABLE');
    setText('hawk-chart-action', 'NO LIVE DECISION');
    setText('hawk-chart-reason', 'Historical evidence explains the move; live evidence earns the entry.');
  }

  bindControls();
  window.HawkEyeChart = { consume, reset };
})();
