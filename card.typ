#let model = json(bytes(sys.inputs.at("card")))

#let ink = rgb("#17231B")
#let muted = rgb("#47584D")
#let primary = rgb("#306442")
#let line-color = rgb("#D8E1D8")
#let surface = rgb("#FCFDF9")
#let surface-low = rgb("#EFF3ED")
#let primary-container = rgb("#DFEEE1")
#let mono = ("Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono")

#set page(width: 144mm, height: auto, margin: 5mm, fill: rgb("#E9EEE8"))
#set text(
  font: ("Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", "DejaVu Sans"),
  size: 11pt, lang: "zh", fill: ink,
)
#set par(leading: 0.4em)
#set block(above: 0pt, below: 0pt)

#let glyph-path = (
  server: "<rect x='4' y='4' width='16' height='7' rx='1.5'/><rect x='4' y='13' width='16' height='7' rx='1.5'/><path d='M8 7.5h.01M8 16.5h.01'/>",
  person: "<circle cx='12' cy='8' r='3.2'/><path d='M5 20v-1.4a7 7 0 0 1 14 0V20'/>",
  pulse: "<path d='M3 12h4l3-7 4 14 3-7h4'/>",
  speed: "<path d='M4.2 17a9 9 0 1 1 15.6 0M12 13l4.4-4.4'/><circle cx='12' cy='13' r='1'/>",
  memory: "<rect x='6' y='6' width='12' height='12' rx='2'/><path d='M9 9h6v6H9zM9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3'/>",
  time: "<circle cx='12' cy='12' r='8.5'/><path d='M12 7v5l3.5 2'/>",
  health: "<path d='M20.2 5.8a4.6 4.6 0 0 0-6.5 0L12 7.5l-1.7-1.7a4.6 4.6 0 0 0-6.5 6.5L12 20l8.2-7.7a4.6 4.6 0 0 0 0-6.5Z'/>",
  latency: "<path d='M4 19h1m4-4v4m5-8v8m5-13v13'/>",
  location: "<path d='M19 10c0 5.1-7 10-7 10s-7-4.9-7-10a7 7 0 0 1 14 0Z'/><circle cx='12' cy='10' r='2.2'/>",
)

#let glyph(name, size: 13pt) = image(
  bytes(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='#306442' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'>"
      + glyph-path.at(name) + "</svg>"
  ),
  format: "svg", width: size, height: size,
)

#let picture(png, fallback, size, radius: 50%) = block(
  width: size, height: size, radius: radius, fill: primary-container, clip: true,
  if png == none {
    align(center + horizon, glyph(fallback, size: size * 0.54))
  } else {
    let raw = bytes(range(0, png.len(), step: 2).map(i => int(png.slice(i, i + 2), base: 16)))
    image(raw, format: "png", width: size, height: size)
  },
)

#let sheet(body) = block(width: 100%, inset: 14pt, radius: 15pt, fill: surface, body)
#let title(value) = text(size: 20pt, weight: "bold", value)
#let caption(value) = text(size: 10pt, fill: muted, value)
#let figure(value, size: 19pt) = text(font: mono, weight: "bold", size: size, str(value))

#let identity(name, subtitle, asset, fallback, radius: 50%) = grid(
  columns: (39pt, 1fr), gutter: 10pt, align: horizon,
  picture(asset, fallback, 39pt, radius: radius),
  stack(spacing: 4pt, title(name), caption(subtitle)),
)

#let info-label(icon, label) = grid(
  columns: (13pt, auto), gutter: 5pt, align: horizon,
  glyph(icon), caption(label),
)

#let metric(icon, name, value, unit) = stack(
  spacing: 6pt,
  info-label(icon, name),
  if icon == "time" {
    text(size: 15pt, weight: "semibold", value)
  } else {
    [#figure(value)#h(4pt)#caption(unit)]
  },
)

#let status-card(m) = sheet[
  #identity(m.title, if m.availability == "" { m.version } else { m.version + " · " + m.availability }, m.icon, "server", radius: 9pt)
  #v(13pt)
  #block(width: 100%, inset: 11pt, radius: 10pt, fill: primary-container)[
    #grid(
      columns: (1fr, auto), align: horizon,
      text(size: 11pt, weight: "semibold", fill: primary, if m.proxy_count { "代理在线" } else { "在线玩家" }),
      [#figure(m.online, size: 21pt)#if not m.proxy_count and m.capacity != none { box([#h(5pt)#caption("/ " + str(m.capacity))]) }],
    )
    #if not m.proxy_count and m.capacity != none and m.capacity > 0 {
      let occupancy = calc.clamp(m.online / m.capacity, 0, 1)
      v(6pt)
      block(width: 100%, height: 4pt, radius: 3pt, clip: true, fill: rgb("#BED3C2"))[
        #if occupancy > 0 {
          block(width: occupancy * 100%, height: 100%, radius: 3pt, fill: primary)
        }
      ]
    }
  ]
  #if m.metrics.len() > 0 {
    v(12pt)
    grid(
      columns: (1fr, 1fr), column-gutter: 12pt, row-gutter: 12pt,
      ..m.metrics.map(row => metric(..row)),
    )
  }
  #if m.motd != "" or "reported_players" in m {
    v(10pt)
    let details = (
      if m.motd != "" { (caption(m.motd),) } else { () }
    ) + (
      if "reported_players" in m { (caption("子服报告 " + m.reported_players),) } else { () }
    )
    stack(spacing: 6pt, ..details)
  }
  #if m.servers.len() > 0 {
    v(11pt)
    caption("子服探测")
    v(5pt)
    stack(spacing: 4pt, ..m.servers.map(((name, detail)) =>
      block(width: 100%, inset: 7pt, radius: 6pt, fill: surface-low, grid(
        columns: (1fr, auto), gutter: 6pt, align: (left, right),
        text(weight: "semibold", name), caption(detail),
      )),
    ))
  }
]

#let player-name(name) = text(weight: "semibold", size: 10.5pt, name)

#let roster-item(player) = block(width: 100%, inset: 6pt, radius: 8pt, fill: surface-low)[
  #grid(
    columns: (24pt, 1fr, auto), gutter: 7pt, align: (horizon, horizon, right + horizon),
    picture(player.avatar, "person", 24pt),
    if player.server == "" { player-name(player.name) } else {
      stack(spacing: 2pt, player-name(player.name), caption(player.server))
    },
    caption(if player.ping == none { "—" } else { player.ping }),
  )
]

#let roster-card(m) = sheet[
  #grid(
    columns: (1fr, auto), column-gutter: 8pt, align: horizon,
    stack(spacing: 4pt, title("在线玩家"), caption(m.title)),
    block(inset: (x: 9pt, y: 5pt), radius: 8pt, fill: primary-container)[
      #text(fill: primary, weight: "semibold", str(m.online) + " 人")
    ],
  )
  #v(11pt)
  #if m.players.len() == 0 {
    block(width: 100%, inset: 11pt, radius: 8pt, fill: surface-low, caption("当前没有在线玩家"))
  } else {
    layout(size => {
      let text-width = (size.width - 7pt) / 2 - 50pt
      let wide = m.players.any(player =>
        measure(player-name(player.name)).width + measure(caption(if player.ping == none { "—" } else { player.ping })).width > text-width)
      grid(columns: if wide { 1 } else { 2 }, gutter: 7pt, ..m.players.map(roster-item))
    })
  }
  #if m.remaining > 0 {
    v(8pt)
    align(center, caption("另有 " + str(m.remaining) + " 名在线玩家"))
  }
]

#let game-modes = (SURVIVAL: "生存", CREATIVE: "创造", ADVENTURE: "冒险", SPECTATOR: "旁观")

#let player-card(m) = sheet[
  #identity(m.title, m.server, m.avatar, "person")
  #if m.health != none or m.ping != none {
    v(12pt)
    let stats = (
      if m.health != none { (metric("health", "生命值", m.health, ""),) } else { () }
    ) + (
      if m.ping != none { (metric("latency", "延迟", m.ping, "ms"),) } else { () }
    )
    block(width: 100%, inset: 11pt, radius: 10pt, fill: surface-low,
      grid(columns: stats.len(), gutter: 10pt, ..stats))
  }
  #if m.world != none and m.mode != none {
    v(11pt)
    grid(
      columns: (1fr, 1fr), gutter: 10pt,
      stack(spacing: 4pt, caption("世界"), text(weight: "semibold", m.world)),
      stack(spacing: 4pt, caption("游戏模式"), text(weight: "semibold", game-modes.at(m.mode, default: m.mode))),
    )
  }
  #if m.coordinates.len() == 3 {
    v(10pt)
    block(width: 100%, inset: 9pt, radius: 9pt, stroke: 0.7pt + line-color)[
    #info-label("location", "坐标")
    #v(6pt)
    #grid(
      columns: (1fr, 1fr, 1fr), gutter: 5pt,
      ..("X", "Y", "Z").zip(m.coordinates).map(((axis, coordinate)) => [
        #caption(axis)#h(5pt)#figure(coordinate, size: 12pt)
      ]),
    )
    ]
  }
]

#(status: status-card, roster: roster-card, player: player-card).at(model.layout)(model)
