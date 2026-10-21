"""stock_page.py - Markup halaman 'Saham' (section + CSS + JS) untuk dashboard Westpro.

Disuntikkan ke HTML_DASHBOARD_TEMPLATE lewat placeholder __SAHAM_PAGE__ (replace biasa, jadi tidak ada masalah escape).
"""

PAGE_HTML = r"""
    <div id="page-saham" class="view-section">
      <style>
        #page-saham .sh-card { background: var(--gf-panel); border: 1px solid var(--gf-border); border-radius: 12px; padding: 0.9rem 1rem; }
        #page-saham .sh-muted { color: var(--gf-text-muted); }
        #page-saham .sh-tabs { display: flex; gap: 0.4rem; flex-wrap: wrap; margin: 0.9rem 0; }
        #page-saham .sh-tab { background: rgba(255,255,255,0.04); color: #a1a1aa; border: 1px solid #353540; border-radius: 8px; padding: 0.4rem 0.9rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; }
        #page-saham .sh-tab.active { color: #fff; background: rgba(16,185,129,0.15); border-color: rgba(16,185,129,0.5); }
        #page-saham .sh-pane { display: none; }
        #page-saham .sh-pane.active { display: block; }
        #page-saham .sh-glob { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 0.2rem; }
        #page-saham .sh-g { background: var(--gf-panel); border: 1px solid var(--gf-border); border-radius: 10px; padding: 0.4rem 0.7rem; min-width: 112px; }
        #page-saham .sh-g b { display: block; font-size: 0.95rem; color: #fff; }
        #page-saham .sh-up { color: #34d399; } #page-saham .sh-down { color: #f87171; } #page-saham .sh-flat { color: #a1a1aa; }
        #page-saham .sh-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 0.9rem; }
        @media (max-width: 520px) { #page-saham .sh-grid { grid-template-columns: 1fr; } }
        #page-saham .sh-row { display: grid; grid-template-columns: 1.4rem 1fr auto; gap: 0.5rem; padding: 0.45rem 0.3rem; border-top: 1px solid var(--gf-border); cursor: pointer; align-items: start; }
        #page-saham .sh-row:hover { background: var(--gf-panel-hover); }
        #page-saham .sh-tk { font-weight: 700; color: #fff; font-size: 0.88rem; }
        #page-saham .sh-nm { font-size: 0.7rem; color: #71717a; }
        #page-saham .sh-rs { font-size: 0.7rem; color: #a1a1aa; margin-top: 0.1rem; }
        #page-saham .sh-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; background: #52525b; }
        #page-saham .sh-dot.positif { background: #34d399; } #page-saham .sh-dot.negatif { background: #f87171; } #page-saham .sh-dot.netral { background: #fbbf24; }
        #page-saham table.sh-t { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
        #page-saham table.sh-t th { text-align: left; color: #a1a1aa; font-weight: 600; padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--gf-border); position: sticky; top: 0; background: var(--gf-panel); cursor: pointer; white-space: nowrap; }
        #page-saham table.sh-t td { padding: 0.35rem 0.5rem; border-bottom: 1px solid #1c1c22; white-space: nowrap; }
        #page-saham table.sh-t tr:hover td { background: var(--gf-panel-hover); }
        #page-saham .sh-scroll { overflow: auto; max-height: 70vh; }
        #page-saham .sh-bar { height: 6px; border-radius: 3px; background: #27272a; overflow: hidden; min-width: 70px; }
        #page-saham .sh-bar > i { display: block; height: 100%; background: linear-gradient(90deg, #10b981, #34d399); }
        #page-saham .sh-badge { display: inline-block; font-size: 0.65rem; padding: 0.1rem 0.45rem; border-radius: 8px; background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.4); margin-left: 0.4rem; }
        #page-saham .sh-progress { display: none; background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.35); color: #7dd3fc; border-radius: 10px; padding: 0.55rem 0.8rem; font-size: 0.8rem; margin-bottom: 0.8rem; }
        #page-saham .sh-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.65); display: none; align-items: center; justify-content: center; z-index: 9999; padding: 1rem; }
        #page-saham .sh-modal.open { display: flex; }
        #page-saham .sh-modal > div { background: var(--gf-canvas); border: 1px solid var(--gf-border-highlight); border-radius: 14px; max-width: 760px; width: 100%; max-height: 88vh; overflow: auto; padding: 1rem 1.1rem; }
        #page-saham .sh-kv { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 0.4rem; margin: 0.6rem 0; }
        #page-saham .sh-kv div { background: var(--gf-panel); border: 1px solid var(--gf-border); border-radius: 8px; padding: 0.3rem 0.5rem; font-size: 0.72rem; color: #a1a1aa; }
        #page-saham .sh-kv b { display: block; color: #fff; font-size: 0.85rem; }
        #page-saham input.sh-in, #page-saham select.sh-in { background: #121318; color: #e4e4e7; border: 1px solid #334155; border-radius: 8px; padding: 0.4rem 0.6rem; font-size: 0.8rem; }
        #page-saham a.sh-link { color: #7dd3fc; text-decoration: none; } #page-saham a.sh-link:hover { text-decoration: underline; }
        #page-saham .sh-out { display: inline-block; font-size: 0.64rem; font-weight: 700; padding: 0.05rem 0.4rem; border-radius: 6px; border: 1px solid #444; margin-bottom: 0.1rem; }
        #page-saham .sh-out.tp { color: #34d399; border-color: rgba(52,211,153,0.5); background: rgba(52,211,153,0.1); }
        #page-saham .sh-out.cl { color: #f87171; border-color: rgba(248,113,113,0.5); background: rgba(248,113,113,0.1); }
        #page-saham .sh-out.flat { color: #a1a1aa; }
        #page-saham table.sh-mx { border-collapse: separate; border-spacing: 0; font-size: 0.74rem; width: 100%; }
        #page-saham table.sh-mx th { position: sticky; top: 0; z-index: 2; background: var(--gf-panel); color: #a1a1aa; padding: 0.45rem 0.4rem; border-bottom: 1px solid var(--gf-border-highlight); font-weight: 600; text-align: center; min-width: 92px; vertical-align: bottom; }
        #page-saham table.sh-mx th.sh-l, #page-saham table.sh-mx td.sh-l { position: sticky; left: 0; z-index: 3; background: var(--gf-panel); text-align: left; min-width: 190px; max-width: 230px; border-right: 1px solid var(--gf-border-highlight); }
        #page-saham table.sh-mx th.sh-l { z-index: 4; }
        #page-saham table.sh-mx td { padding: 0.3rem 0.35rem; border-bottom: 1px solid #1c1c22; text-align: center; white-space: nowrap; }
        #page-saham table.sh-mx td.sh-l { cursor: pointer; white-space: normal; }
        #page-saham table.sh-mx tr:hover td { background: var(--gf-panel-hover); }
        #page-saham table.sh-mx td.sh-c { cursor: pointer; background: rgba(56,189,248,0.07); }
        #page-saham table.sh-mx td.sh-c b { color: #7dd3fc; font-size: 0.8rem; }
        #page-saham table.sh-mx td.sh-c.tp { background: rgba(52,211,153,0.16); } #page-saham table.sh-mx td.sh-c.tp b { color: #34d399; }
        #page-saham table.sh-mx td.sh-c.cl { background: rgba(248,113,113,0.16); } #page-saham table.sh-mx td.sh-c.cl b { color: #f87171; }
        #page-saham table.sh-mx td.sh-c.nt { background: rgba(161,161,170,0.1); } #page-saham table.sh-mx td.sh-c.nt b { color: #d4d4d8; }
        #page-saham table.sh-mx th.sh-r, #page-saham table.sh-mx td.sh-r { position: sticky; right: 0; z-index: 3; background: var(--gf-panel); border-left: 1px solid var(--gf-border-highlight); min-width: 118px; }
        #page-saham table.sh-mx th.sh-r { z-index: 4; }
        #page-saham table.sh-mx th.sh-rd, #page-saham table.sh-mx td.sh-rd { min-width: 70px; }
        #page-saham .sh-verdict { display: inline-block; font-weight: 800; font-size: 0.74rem; padding: 0.12rem 0.5rem; border-radius: 6px; border: 1px solid #444; }
        #page-saham .sh-verdict.hit { color: #34d399; border-color: rgba(52,211,153,0.6); background: rgba(52,211,153,0.12); }
        #page-saham .sh-verdict.miss { color: #f87171; border-color: rgba(248,113,113,0.6); background: rgba(248,113,113,0.12); }
        #page-saham .sh-verdict.wait { color: #a1a1aa; }
        #page-saham .sh-c2 { font-size: 0.64rem; color: #a1a1aa; }
        #page-saham .sh-seg { display: inline-flex; border: 1px solid #353540; border-radius: 8px; overflow: hidden; }
        #page-saham .sh-seg button { background: transparent; color: #a1a1aa; border: 0; padding: 0.35rem 0.8rem; font-size: 0.76rem; font-weight: 600; cursor: pointer; }
        #page-saham .sh-seg button.active { background: rgba(16,185,129,0.18); color: #fff; }
        #page-saham .sh-cnt { display: inline-block; min-width: 1.3rem; text-align: center; font-weight: 700; font-size: 0.72rem; border-radius: 6px; padding: 0.05rem 0.3rem; background: rgba(168,85,247,0.2); color: #d8b4fe; }
        #page-saham .sh-disclaimer { font-size: 0.7rem; color: #71717a; margin-top: 1rem; }
      </style>

      <div class="page-header" style="margin-bottom: 0.8rem; display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:0.75rem;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
            <h1 style="font-size:1.25rem; font-weight:700; color:#fff; margin:0;">Analisis Saham IHSG</h1>
            <span id="shSchedBadge" class="hermes-badge badge-warn">MEMUAT...</span>
          </div>
          <div class="sh-muted" style="font-size:0.8rem; margin-top:0.25rem;">
            Data live semua saham IDX &bull; 8 metode analisis + AI &amp; berita &bull; Pra-pasar 08:20 WIB &bull; TP/CL per saham &bull; Evaluasi akurasi 16:30 WIB (Senin&ndash;Jumat)
          </div>
        </div>
        <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
          <button class="hb-btn hb-btn-primary" id="shBtnPre" onclick="shRun('pre')">&#9654; Jalankan analisis sekarang (uji)</button>
          <button class="hb-btn" id="shBtnClose" onclick="shRun('close')">&#9654; Evaluasi penutupan sekarang</button>
        </div>
      </div>

      <div id="shProgress" class="sh-progress"></div>
      <div id="shGlobal" class="sh-glob"></div>

      <div class="sh-tabs">
        <button class="sh-tab active" data-pane="rec" onclick="shTab('rec')">Rekomendasi</button>
        <button class="sh-tab" data-pane="acc" onclick="shTab('acc')">Akurasi Metode</button>
        <button class="sh-tab" data-pane="sim" onclick="shTab('sim')">Simulasi AI Trading</button>
        <button class="sh-tab" data-pane="all" onclick="shTab('all')">Semua Saham (Live)</button>
      </div>

      <div id="shPane-rec" class="sh-pane active">
        <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin-bottom:0.8rem;">
          <span class="sh-muted" style="font-size:0.8rem;">Sesi analisis:</span>
          <select id="shRunSel" class="sh-in" onchange="shLoadPicks()"></select>
          <span id="shRunNote" class="sh-muted" style="font-size:0.75rem;"></span>
          <span id="shLiveNote" class="sh-muted" style="font-size:0.75rem;"></span>
          <div class="sh-seg" style="margin-left:auto;"><button id="shVMx" class="active" onclick="shView('matrix')">Matriks</button><button id="shVCd" onclick="shView('cards')">Kartu</button></div>
          <span class="sh-muted" style="font-size:0.78rem;" id="shMinWrap">Tampilkan saham yang dipilih &ge;
            <select id="shMin" class="sh-in" onchange="renderMatrixNow()"><option value="1">1 metode</option><option value="2">2 metode</option><option value="3">3 metode</option><option value="4">4 metode</option></select></span>
        </div>
        <div id="shOverview"></div>
        <div id="shEval"></div>
        <div id="shMatrix"></div>
        <div id="shMethods" class="sh-grid" style="display:none;"></div>
      </div>

      <div id="shPane-acc" class="sh-pane">
        <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin-bottom:0.8rem;">
          <span class="sh-muted" style="font-size:0.8rem;">Periode:</span>
          <select id="shWin" class="sh-in" onchange="shLoadStats()">
            <option value="7">7 hari</option><option value="30">30 hari</option><option value="all" selected>Semua</option>
          </select>
        </div>
        <div id="shAcc"></div>
        <h3 style="color:#fff; font-size:0.95rem; margin:1.2rem 0 0.5rem;">Riwayat harian (hit-rate per metode)</h3>
        <div id="shHist" class="sh-card sh-scroll"></div>
      </div>

      <div id="shPane-sim" class="sh-pane">
        <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin-bottom:0.8rem;">
          <div class="sh-seg"><button id="shSimBtnLive" class="active" onclick="shSimAcct('live')">Akun Live</button><button id="shSimBtnTest" onclick="shSimAcct('test')">Akun Uji</button></div>
          <span id="shSimNote" class="sh-muted" style="font-size:0.75rem;"></span>
        </div>
        <div class="sh-card" style="margin-bottom:0.9rem;">
          <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:0.6rem; align-items:end;">
            <label class="sh-muted" style="font-size:0.72rem;">Modal awal (Rp)<input id="shSimCap" class="sh-in" type="number" min="1000000" step="1000000" style="width:100%; box-sizing:border-box;"></label>
            <label class="sh-muted" style="font-size:0.72rem;">Maks posisi<input id="shSimMaxPos" class="sh-in" type="number" min="1" max="15" style="width:100%; box-sizing:border-box;"></label>
            <label class="sh-muted" style="font-size:0.72rem;">Maks % ekuitas / saham<input id="shSimMaxPct" class="sh-in" type="number" min="5" max="100" style="width:100%; box-sizing:border-box;"></label>
            <label class="sh-muted" style="font-size:0.72rem;">Fee beli %<input id="shSimFeeB" class="sh-in" type="number" step="0.01" style="width:100%; box-sizing:border-box;"></label>
            <label class="sh-muted" style="font-size:0.72rem;">Fee jual %<input id="shSimFeeS" class="sh-in" type="number" step="0.01" style="width:100%; box-sizing:border-box;"></label>
            <label class="sh-muted" style="font-size:0.78rem;"><input type="checkbox" id="shSimEnabled"> AI trading otomatis (akun live)</label>
          </div>
          <div style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.7rem;">
            <button class="hb-btn hb-btn-primary" onclick="shSimSave()">Simpan pengaturan</button>
            <button class="hb-btn" onclick="shSimReset()">Reset akun &amp; mulai dengan modal ini</button>
            <button class="hb-btn" id="shSimBtnTick" style="display:none;" onclick="shSimRun('tick')">&#9654; Jalankan 1 siklus AI sekarang</button>
            <button class="hb-btn" id="shSimBtnLiq" style="display:none;" onclick="shSimRun('liquidate')">&#9632; Jual semua &amp; akhiri hari</button>
          </div>
          <div class="sh-muted" style="font-size:0.7rem; margin-top:0.5rem;">AI mengecek data tiap 10 menit (09:10&ndash;15:50 WIB), memutuskan beli/jual/tahan sesuai dana, lalu semua posisi dilikuidasi 15:50 (day trading). Beli di harga ask, jual di harga bid (&plusmn;1 tick dari harga terakhir; order book asli tidak tersedia di data gratis), fee dihitung. Ini simulasi: antrian order, ARA/ARB, dan partial fill tidak dimodelkan.</div>
        </div>
        <div id="shSimSummary" class="sh-glob" style="margin-bottom:0.9rem;"></div>
        <div class="sh-card" style="margin-bottom:0.9rem;"><b style="color:#fff; font-size:0.85rem;">Kurva ekuitas hari ini</b><div id="shSimChart" style="margin-top:0.4rem;"></div></div>
        <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(440px,1fr)); gap:0.9rem; margin-bottom:0.9rem;">
          <div class="sh-card"><b style="color:#fff; font-size:0.85rem;">Posisi saat ini</b><div class="sh-scroll" style="max-height:40vh;" id="shSimPos"></div></div>
          <div class="sh-card"><b style="color:#fff; font-size:0.85rem;">Transaksi hari ini</b><div class="sh-scroll" style="max-height:40vh;" id="shSimTrades"></div></div>
        </div>
        <div class="sh-card" style="margin-bottom:0.9rem;"><b style="color:#fff; font-size:0.85rem;">Log keputusan AI</b><div class="sh-scroll" style="max-height:34vh; margin-top:0.3rem;" id="shSimLog"></div></div>
        <div class="sh-card" style="margin-bottom:0.9rem;">
          <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin-bottom:0.6rem;">
            <b style="color:#fff; font-size:0.95rem;">Laporan realized gain / loss &amp; grafik hari ke hari</b>
            <select id="shRpPeriod" class="sh-in" onchange="shSimReport()" style="margin-left:auto;">
              <option value="all">Sejak awal sampai sekarang</option><option value="7">7 hari terakhir</option><option value="30">30 hari terakhir</option><option value="custom">Rentang kustom...</option>
            </select>
            <span id="shRpCustom" style="display:none;"><input id="shRpFrom" type="date" class="sh-in"> &ndash; <input id="shRpTo" type="date" class="sh-in"> <button class="hb-btn" onclick="shSimReport()">Terapkan</button></span>
          </div>
          <div id="shRpSummary" class="sh-glob" style="margin-bottom:0.8rem;"></div>
          <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:0.9rem;">
            <div><div class="sh-muted" style="font-size:0.75rem;">Ekuitas dari hari ke hari (akhir hari)</div><div id="shRpEq"></div></div>
            <div><div class="sh-muted" style="font-size:0.75rem;">Profit / loss per hari (Rp)</div><div id="shRpBars"></div></div>
          </div>
        </div>
        <h3 style="color:#fff; font-size:0.95rem; margin:0.6rem 0 0.5rem;">Riwayat harian <span class="sh-muted" style="font-size:0.72rem; font-weight:400;">(klik baris untuk melihat grafik intraday &amp; transaksi hari itu)</span></h3>
        <div class="sh-card sh-scroll" id="shSimHist" style="margin-bottom:0.6rem;"></div>
        <div id="shSimDay"></div>
        <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin:1rem 0 0.5rem;">
          <h3 style="color:#fff; font-size:0.95rem; margin:0;">Jurnal transaksi (dari awal sampai akhir)</h3>
          <input id="shJrTicker" class="sh-in" placeholder="Filter kode saham" style="width:130px; margin-left:auto;" onkeydown="if(event.key==='Enter') shSimJournal(true)">
          <select id="shJrOrder" class="sh-in" onchange="shSimJournal(true)"><option value="asc">Terlama dulu</option><option value="desc">Terbaru dulu</option></select>
          <button class="hb-btn" onclick="shSimJournal(true)">Terapkan</button>
          <button class="hb-btn" onclick="shSimCsv()">&#8681; Ekspor CSV</button>
        </div>
        <div class="sh-card sh-scroll" id="shJournal" style="margin-bottom:0.6rem;"></div>
        <div class="sh-card" id="shPerTicker" style="margin-bottom:0.6rem;"></div>
      </div>

      <div id="shPane-all" class="sh-pane">
        <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin-bottom:0.8rem;">
          <input id="shQ" class="sh-in" placeholder="Cari kode / nama emiten..." oninput="shRenderAll()" style="min-width:220px;">
          <select id="shSector" class="sh-in" onchange="shRenderAll()"><option value="">Semua sektor</option></select>
          <label class="sh-muted" style="font-size:0.78rem;"><input type="checkbox" id="shLiq" onchange="shRenderAll()" checked> hanya likuid</label>
          <button class="hb-btn" onclick="shLoadAll(true)">&#8635; Refresh data live</button>
          <span id="shAllNote" class="sh-muted" style="font-size:0.75rem;"></span>
        </div>
        <div class="sh-card sh-scroll"><table class="sh-t" id="shAllTbl"></table></div>
      </div>

      <div class="sh-disclaimer">
        Bukan nasihat investasi. Rekomendasi dicatat otomatis sebelum pasar buka lalu dievaluasi terhadap harga penutupan; TP/CL dihitung dari ATR(14) per metode untuk horizon 1 hari bursa; &ldquo;hit&rdquo; berarti harga tertinggi hari itu menyentuh TP (bila TP dan CL sama-sama tersentuh dihitung CL).
        Data TradingView tertunda 10 menit (resmi, kebijakan BEI untuk data gratis). Berita dari Google News &amp; RSS media; analisis AI dapat keliru.
      </div>

      <div id="shModal" class="sh-modal" onclick="if(event.target===this) shClose()">
        <div id="shModalBody"></div>
      </div>

      <script>
      (function () {
        var SH = { picks: null, all: [], sortKey: 'value', sortDir: -1, poll: null, loaded: false, allShown: 150 };
        var nf = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 0 });
        var nf1 = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 1 });
        var nf2 = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 2 });
        function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
        function num(v, f) { return (v == null || isNaN(v)) ? '-' : (f || nf).format(v); }
        function pct(v) { if (v == null || isNaN(v)) return '<span class="sh-flat">-</span>'; var c = v > 0 ? 'sh-up' : (v < 0 ? 'sh-down' : 'sh-flat'); return '<span class="' + c + '">' + (v > 0 ? '+' : '') + v.toFixed(2) + '%</span>'; }
        function big(v) { if (v == null) return '-'; var a = Math.abs(v); if (a >= 1e12) return nf1.format(v / 1e12) + ' T'; if (a >= 1e9) return nf1.format(v / 1e9) + ' M'; if (a >= 1e6) return nf1.format(v / 1e6) + ' jt'; return nf.format(v); }
        function api(path, opt) { return fetch('/api/saham/' + path, opt).then(function (r) { return r.json(); }); }

        window.shTab = function (p) {
          document.querySelectorAll('#page-saham .sh-tab').forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-pane') === p); });
          document.querySelectorAll('#page-saham .sh-pane').forEach(function (d) { d.classList.remove('active'); });
          document.getElementById('shPane-' + p).classList.add('active');
          if (p === 'acc') shLoadStats();
          if (SH.simPoll) { clearInterval(SH.simPoll); SH.simPoll = null; }
          if (p === 'sim') { SH.simAcct = SH.simAcct || 'live'; shSimLoad(true); shSimJournal(true); SH.simPoll = setInterval(function () { shSimLoad(false); }, 15000); }
          if (p === 'all' && !SH.all.length) shLoadAll(false);
        };

        window.shOnShow = function () {
          shStatus(); shGlobal(); shLoadPicks();
          if (SH.poll) clearInterval(SH.poll);
          SH.poll = setInterval(shStatus, 4000);
        };
        window.shStopPolling = function () { if (SH.poll) { clearInterval(SH.poll); SH.poll = null; } if (SH.simPoll) { clearInterval(SH.simPoll); SH.simPoll = null; } };

        function shStatus() {
          api('status').then(function (d) {
            if (!d || !d.state) return;
            var s = d.state, b = document.getElementById('shSchedBadge'), p = document.getElementById('shProgress');
            var wk = d.weekday < 5;
            b.className = 'hermes-badge ' + (d.scheduler_alive ? 'badge-ok' : 'badge-warn');
            b.textContent = d.scheduler_alive ? (wk ? 'JADWAL AKTIF' : 'AKHIR PEKAN') : 'SCHEDULER MATI';
            if (s.running) {
              p.style.display = 'block';
              p.innerHTML = '&#9203; Job <b>' + esc(s.job === 'pre' ? 'analisis pra-pasar' : 'evaluasi penutupan') + '</b> berjalan: ' + esc(s.stage);
            } else {
              if (p.style.display === 'block' && !s.error) { shLoadPicks(); }
              p.style.display = s.error ? 'block' : 'none';
              if (s.error) p.innerHTML = '&#9888;&#65039; Job terakhir gagal: ' + esc(s.error);
            }
            var ln = document.getElementById('shLiveNote');
            if (d.market_hours && d.live) {
              var t = d.live.last ? new Date(d.live.last * 1000).toLocaleTimeString('id-ID', { timeZone: 'Asia/Jakarta', hour: '2-digit', minute: '2-digit' }) : '-';
              ln.innerHTML = '<span style="color:#34d399;">&#9679;</span> Pelacak TP/CL live aktif &bull; cek terakhir ' + t + ' WIB &bull; data tertunda 10 mnt';
              SH.liveTick = (SH.liveTick || 0) + 1;
              if (SH.liveTick >= 15 && SH.picks && SH.picks.date === d.today) { SH.liveTick = 0; shLoadPicks(); }
            } else { ln.textContent = ''; SH.liveTick = 0; }
            document.getElementById('shBtnPre').disabled = s.running;
            document.getElementById('shBtnClose').disabled = s.running;
          }).catch(function () { });
        }

        function shGlobal() {
          api('global').then(function (d) {
            var box = document.getElementById('shGlobal');
            if (!d || !d.global) { box.innerHTML = ''; return; }
            box.innerHTML = d.global.map(function (g) {
              return '<div class="sh-g"><span class="sh-muted" style="font-size:0.7rem;">' + esc(g.label) + '</span><b>' + num(g.last, nf2) + '</b>' + pct(g.chg_pct) + '</div>';
            }).join('');
          }).catch(function () { });
        }

        window.shRun = function (job) {
          if (!confirm(job === 'pre' ? 'Jalankan analisis lengkap sekarang? (memakai AI, sekitar 3-6 menit). Di luar 08:00-09:00 hasilnya ditandai UJI dan tidak masuk statistik akurasi.' : 'Jalankan evaluasi penutupan sekarang?')) return;
          api('run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job: job }) }).then(function (d) {
            if (!d.success) alert(d.error || 'Gagal memulai'); shStatus();
          });
        };

        window.shLoadPicks = function () {
          var sel = document.getElementById('shRunSel'), qs = '';
          if (sel && sel.value) { var p = sel.value.split('|'); qs = '?date=' + p[0] + '&tag=' + p[1]; }
          api('picks' + qs).then(function (d) {
            SH.picks = d; renderRuns(d); renderOverview(d); renderMethods(d); renderMatrix(d);
          });
        };

        function renderRuns(d) {
          var sel = document.getElementById('shRunSel');
          sel.innerHTML = (d.runs || []).map(function (r) { return '<option value="' + r.date + '|' + r.tag + '">' + r.date + (r.tag === 'pre' ? ' (pra-pasar)' : ' (uji)') + '</option>'; }).join('') || '<option value="">Belum ada analisis</option>';
          if (d.date) sel.value = d.date + '|' + d.tag;
          document.getElementById('shRunNote').textContent = d.tag === 'test' ? 'Sesi UJI: tidak dihitung di statistik akurasi.' : '';
        }

        function renderOverview(d) {
          var o = d.overview, box = document.getElementById('shOverview'), ev = document.getElementById('shEval');
          if (!o) { box.innerHTML = '<div class="sh-card sh-muted">Belum ada analisis. Analisis otomatis berjalan 08:20 WIB, atau klik &ldquo;Jalankan analisis sekarang&rdquo;.</div><br>'; ev.innerHTML = ''; return; }
          var out = o.ihsg_outlook || {}, bias = (out.bias || '-').toLowerCase();
          var bc = bias === 'naik' ? 'sh-up' : (bias === 'turun' ? 'sh-down' : 'sh-flat');
          var list = function (a) { return (a || []).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join(''); };
          box.innerHTML = '<div class="sh-card" style="margin-bottom:0.9rem;">' +
            '<div style="display:flex; justify-content:space-between; flex-wrap:wrap; gap:0.5rem;"><b style="color:#fff;">&#129302; Briefing AI</b><span class="sh-muted" style="font-size:0.72rem;">' + esc(o._created_at || '') + ' &bull; ' + num(o.total_stocks) + ' saham dipindai, ' + num(o.universe) + ' likuid</span></div>' +
            (o.ai_error ? '<div class="sh-down" style="font-size:0.78rem; margin-top:0.4rem;">AI gagal: ' + esc(o.ai_error) + ' (metode kuantitatif tetap tersedia)</div>' : '') +
            '<p style="font-size:0.85rem; line-height:1.55; color:#d4d4d8; margin:0.5rem 0;">' + esc(o.market_overview || '-') + '</p>' +
            '<div style="font-size:0.82rem;">Bias IHSG: <b class="' + bc + '">' + esc(bias.toUpperCase()) + '</b> <span class="sh-muted">' + esc(out.reason || '') + '</span>' +
            (out.support || out.resistance ? ' <span class="sh-muted"> &bull; Support ' + esc(out.support || '-') + ' / Resistance ' + esc(out.resistance || '-') + '</span>' : '') + '</div>' +
            '<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:0.8rem; margin-top:0.6rem; font-size:0.78rem; color:#a1a1aa;">' +
            '<div><b style="color:#e4e4e7;">Berita kunci</b><ul style="margin:0.3rem 0 0 1rem; padding:0;">' + list(o.key_news) + '</ul></div>' +
            '<div><b style="color:#e4e4e7;">Sektor dipantau</b><ul style="margin:0.3rem 0 0 1rem; padding:0;">' + list(o.watch_sectors) + '</ul></div>' +
            '<div><b style="color:#e4e4e7;">Risiko</b><ul style="margin:0.3rem 0 0 1rem; padding:0;">' + list(o.risks) + '</ul></div></div></div>';
          var e = d.eval;
          if (e && e.methods) {
            var best = e.methods.filter(function (m) { return m.n > 0; })[0];
            ev.innerHTML = '<div class="sh-card" style="margin-bottom:0.9rem; border-color:rgba(16,185,129,0.4);"><b style="color:#fff;">&#128202; Evaluasi penutupan</b> ' +
              '<span class="sh-muted" style="font-size:0.78rem;">IHSG ' + pct(e.ihsg_ret) + ' &bull; rata-rata saham likuid ' + pct(e.bench_ret) + (best ? ' &bull; metode terbaik hari ini: <b style="color:#34d399;">' + esc(best.label) + '</b> (' + best.hit_rate.toFixed(0) + '% naik)' : '') + '</span>' +
              (e.comment ? '<p style="font-size:0.83rem; line-height:1.5; color:#d4d4d8; margin:0.5rem 0 0;">' + esc(e.comment) + '</p>' : '') + '</div>';
          } else ev.innerHTML = '';
        }

        function lvl(p) {
          if (!p.tp || !p.cl || !p.ref_price) return '';
          var up = (p.tp / p.ref_price - 1) * 100, dn = (p.cl / p.ref_price - 1) * 100;
          return '<span class="sh-up">TP ' + num(p.tp) + ' (+' + up.toFixed(1) + '%)</span> &bull; <span class="sh-down">CL ' + num(p.cl) + ' (' + dn.toFixed(1) + '%)</span>';
        }
        function isEval(p) { return p.status === 'done' || p.status === 'live'; }
        function outBadge(p) {
          if (p.status === 'no_trade') return '<span class="sh-out flat">TDK TRADE</span>';
          if (!isEval(p)) return '';
          var o = p.outcome || 'NETRAL', lv = p.status === 'live' ? ' <i>LIVE</i>' : '';
          if (o === 'TP') return '<span class="sh-out tp">&#9989; HIT TP' + lv + '</span>';
          if (o === 'CL') return '<span class="sh-out cl">&#10060; CL' + lv + '</span>';
          if (o === 'BERJALAN') return '<span class="sh-out flat">&#128993; berjalan</span>';
          return '<span class="sh-out flat">&#10134; NETRAL</span>';
        }
        function sentDot(s) { return '<span class="sh-dot ' + (s || '') + '" title="Sentimen AI: ' + esc(s || 'n/a') + '"></span>'; }

        function renderMethods(d) {
          var box = document.getElementById('shMethods');
          box.innerHTML = (d.methods || []).map(function (m) {
            var rows = m.picks.map(function (p) {
              var res = (isEval(p) ? outBadge(p) + '<div>' + pct(p.result_ret) + '</div>' : outBadge(p));
              return '<div class="sh-row" onclick="shDetail(\'' + m.key + '\',' + p.rank + ')"><span class="sh-muted" style="font-size:0.72rem;">' + p.rank + '</span>' +
                '<div><div class="sh-tk">' + sentDot(p.sentiment) + esc(p.ticker) + ' <span class="sh-nm">' + esc(p.name) + '</span></div><div class="sh-rs">' + esc(p.reason) + '</div><div class="sh-rs">' + lvl(p) + '</div></div>' +
                '<div style="text-align:right; font-size:0.78rem; color:#e4e4e7;">' + num(p.ref_price) + '<div>' + res + '</div></div></div>';
            }).join('') || '<div class="sh-muted" style="font-size:0.78rem; padding:0.6rem 0;">Tidak ada saham yang memenuhi kriteria.</div>';
            var done = m.picks.filter(isEval);
            var tpn = done.filter(function (p) { return p.outcome === 'TP'; }).length, cln = done.filter(function (p) { return p.outcome === 'CL'; }).length;
            var badge = done.length ? '<span class="sh-badge">TP ' + tpn + ' &bull; CL ' + cln + ' &bull; ' + Math.round(done.filter(function (p) { return p.hit; }).length / done.length * 100) + '% naik</span>' : '';
            return '<div class="sh-card"><div style="display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap;"><b style="color:#fff;">' + esc(m.label) + '</b>' + badge + '</div>' +
              '<div class="sh-muted" style="font-size:0.72rem; margin:0.15rem 0 0.4rem;">' + esc(m.desc) + '</div>' + rows + '</div>';
          }).join('');
        }

        window.shView = function (v) {
          SH.view = v;
          document.getElementById('shVMx').classList.toggle('active', v === 'matrix');
          document.getElementById('shVCd').classList.toggle('active', v === 'cards');
          document.getElementById('shMatrix').style.display = v === 'matrix' ? 'block' : 'none';
          document.getElementById('shMinWrap').style.display = v === 'matrix' ? 'inline' : 'none';
          document.getElementById('shMethods').style.display = v === 'cards' ? 'grid' : 'none';
        };
        window.renderMatrixNow = function () { if (SH.picks) renderMatrix(SH.picks); };

        function renderMatrix(d) {
          var box = document.getElementById('shMatrix');
          var methods = d.methods || [], map = {};
          methods.forEach(function (m) {
            m.picks.forEach(function (p) {
              var r = map[p.ticker] || (map[p.ticker] = { ticker: p.ticker, name: p.name, sector: p.sector, sentiment: p.sentiment, ref: p.ref_price, cells: {}, count: 0, rankSum: 0 });
              r.cells[m.key] = p; r.count++; r.rankSum += p.rank;
            });
          });
          var min = parseInt(document.getElementById('shMin').value, 10) || 1;
          var rows = Object.keys(map).map(function (k) { return map[k]; }).filter(function (r) { return r.count >= min; })
            .sort(function (a, b) { return (b.count - a.count) || (a.rankSum / a.count - b.rankSum / b.count); });
          if (!rows.length) { box.innerHTML = '<div class="sh-card sh-muted">Belum ada rekomendasi untuk sesi ini.</div>'; return; }
          var head = '<tr><th class="sh-l">Saham (diurutkan dari paling banyak dipilih)</th>' + methods.map(function (m) { return '<th title="' + esc(m.desc) + '">' + esc(m.label) + '</th>'; }).join('') + '<th class="sh-rd" title="Jumlah metode yang memasukkan saham ini ke radar">Radar</th><th class="sh-r" title="Hasil setelah tutup bursa: HIT bila TP tercapai di minimal satu metode">Hasil</th></tr>';
          var body = rows.map(function (r) {
            var first = methods.filter(function (m) { return r.cells[m.key]; })[0];
            var left = '<td class="sh-l" onclick="shDetail(\'' + first.key + '\',' + r.cells[first.key].rank + ')"><div><span class="sh-cnt" title="Jumlah metode yang memilih">' + r.count + '</span> ' + sentDot(r.sentiment) + '<b class="sh-tk" style="font-size:0.85rem;">' + esc(r.ticker) + '</b> <span class="sh-muted">' + num(r.ref) + '</span></div><div class="sh-nm">' + esc(r.name) + ' &bull; ' + esc(r.sector) + '</div></td>';
            var cells = methods.map(function (m) {
              var p = r.cells[m.key];
              if (!p) return '<td></td>';
              var done = isEval(p), cls = done ? (p.outcome === 'TP' ? 'tp' : (p.outcome === 'CL' ? 'cl' : 'nt')) : '';
              return '<td class="sh-c ' + cls + '" title="' + esc(m.label + ': ' + p.reason + (p.note ? ' | ' + p.note : '')) + '" onclick="shDetail(\'' + m.key + '\',' + p.rank + ')">' +
                '<b>#' + p.rank + '</b>' + (done ? ' ' + (p.outcome === 'TP' ? '&#9989;' : (p.outcome === 'CL' ? '&#10060;' : (p.outcome === 'BERJALAN' ? '&#128993;' : '&#10134;'))) : '') +
                '<div class="sh-c2"><span class="sh-up">' + num(p.tp) + '</span> / <span class="sh-down">' + num(p.cl) + '</span></div>' + (done ? '<div class="sh-c2">' + pct(p.result_ret) + '</div>' : '') + '</td>';
            }).join('');
            var picks = Object.keys(r.cells).map(function (k) { return r.cells[k]; });
            var done = picks.filter(isEval), fin = picks.filter(function (p) { return p.status === 'done'; }), liveNow = picks.some(function (p) { return p.status === 'live'; });
            var tpn = done.filter(function (p) { return p.outcome === 'TP'; }).length, cln = done.filter(function (p) { return p.outcome === 'CL'; }).length;
            var radar = '<td class="sh-rd"><b style="color:#d8b4fe; font-size:0.85rem;">' + r.count + '/' + methods.length + '</b><div class="sh-bar" style="margin-top:3px;"><i style="width:' + (r.count / methods.length * 100) + '%; background:linear-gradient(90deg,#a855f7,#d8b4fe);"></i></div></td>';
            var verdict;
            if (!done.length) {
              verdict = '<span class="sh-verdict wait">&#9203; menunggu</span>';
            } else {
              var ret = done[0].result_ret;
              var head = tpn > 0 ? '<span class="sh-verdict hit">&#9989; HIT' + (liveNow ? ' <i style="font-size:0.6rem;">LIVE</i>' : '') + '</span>'
                : ((fin.length === picks.length || cln === picks.length)
                  ? '<span class="sh-verdict miss">&#10060; MELESET' + (fin.length === picks.length ? '' : ' <i style="font-size:0.6rem;">LIVE</i>') + '</span>'
                  : '<span class="sh-verdict wait">&#128993; berjalan</span>');
              verdict = head + '<div class="sh-c2">TP ' + tpn + '/' + done.length + ' &bull; CL ' + cln + '/' + done.length + '</div><div class="sh-c2">' + (liveNow ? 'kini ' : 'tutup ') + pct(ret) + '</div>';
            }
            return '<tr>' + left + cells + radar + '<td class="sh-r">' + verdict + '</td></tr>';
          }).join('');
          box.innerHTML = '<div class="sh-card sh-scroll" style="padding:0;"><table class="sh-mx"><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>' +
            '<div class="sh-muted" style="font-size:0.72rem; margin-top:0.4rem;">Sel = peringkat dalam metode &bull; hijau/merah kecil = TP / CL &bull; setelah evaluasi sel diwarnai: hijau = TP tercapai, merah = CL, abu = netral. Kolom Radar = berapa metode (dari ' + methods.length + ') yang memasukkan saham ke radar. Hasil: HIT = TP tercapai di minimal satu metode (diperbarui live selama jam bursa), MELESET = sampai tutup tidak ada TP yang tercapai. Klik sel atau saham untuk detail &amp; berita.</div>';
        }

        function rp(v) { return (v == null || isNaN(v)) ? '-' : (v < 0 ? '-Rp' : 'Rp') + nf.format(Math.abs(v)); }
        function pnlc(v) { return v > 0 ? 'sh-up' : (v < 0 ? 'sh-down' : 'sh-flat'); }
        window.shSimAcct = function (a) {
          SH.simAcct = a;
          document.getElementById('shSimBtnLive').classList.toggle('active', a === 'live');
          document.getElementById('shSimBtnTest').classList.toggle('active', a === 'test');
          document.getElementById('shSimBtnTick').style.display = a === 'test' ? 'inline-block' : 'none';
          document.getElementById('shSimBtnLiq').style.display = a === 'test' ? 'inline-block' : 'none';
          shSimLoad(true); shSimJournal(true);
        };
        window.shSimLoad = function (fill) {
          api('sim/state?acct=' + (SH.simAcct || 'live')).then(function (d) { if (d && d.success) shSimRender(d, fill); });
        };
        function shSimRender(d, fill) {
          var c = d.config, a = d.account, t = d.today || {};
          if (fill || !SH.simFilled) {
            document.getElementById('shSimCap').value = c.capital; document.getElementById('shSimMaxPos').value = c.max_positions;
            document.getElementById('shSimMaxPct').value = c.max_pos_pct; document.getElementById('shSimFeeB').value = c.fee_buy;
            document.getElementById('shSimFeeS').value = c.fee_sell; document.getElementById('shSimEnabled').checked = !!c.enabled; SH.simFilled = true;
          }
          var rt = d.runtime || {};
          document.getElementById('shSimNote').innerHTML = (d.acct === 'test' ? 'Akun UJI: bisa dijalankan kapan saja, terpisah dari akun live. ' : (c.enabled ? '<span style="color:#34d399;">&#9679;</span> AI trading otomatis AKTIF. ' : 'AI trading otomatis NONAKTIF (centang lalu simpan). ')) +
            (rt.running ? '&#9203; ' + esc(rt.stage || 'siklus berjalan...') + ' ' : '') + (rt.last_tick_at ? 'Siklus terakhir ' + esc(rt.last_tick_at) + ' ' : '') + (d.ticks ? '&bull; data per menit hari ini: ' + d.ticks.points + ' titik / ' + d.ticks.tickers + ' saham' + (d.ticks.last ? ' (terakhir ' + esc(d.ticks.last) + ')' : '') + ' ' : '') + (rt.last_error ? '<span class="sh-down">' + esc(rt.last_error).slice(0, 120) + '</span>' : '');
          var card = function (label, val, sub, cls) { return '<div class="sh-g" style="min-width:150px;"><span class="sh-muted" style="font-size:0.7rem;">' + label + '</span><b class="' + (cls || '') + '">' + val + '</b>' + (sub || '') + '</div>'; };
          var st = d.stats;
          document.getElementById('shSimSummary').innerHTML =
            card('Modal awal', rp(a.initial)) + card('Ekuitas kini', rp(a.equity), '', pnlc(a.total_pnl)) + card('Kas', rp(a.cash)) +
            card('P&amp;L hari ini', rp(t.pnl), '<div>' + pct(t.pnl_pct) + '</div>', pnlc(t.pnl || 0)) +
            card('P&amp;L total', rp(a.total_pnl), '<div>' + pct(a.total_pnl_pct) + '</div>', pnlc(a.total_pnl)) +
            card('Hari profit', st.win_days + ' / ' + st.days, '<div class="sh-muted" style="font-size:0.7rem;">rata-rata ' + (st.avg_daily_pct >= 0 ? '+' : '') + st.avg_daily_pct + '%/hari</div>') +
            card('Win / Loss hari ini', (t.wins || 0) + ' / ' + (t.losses || 0), '<div class="sh-muted" style="font-size:0.7rem;">' + (t.trades || 0) + ' transaksi jual</div>');
          // kurva
          var cv = d.curve || [], ch = document.getElementById('shSimChart');
          if (cv.length < 2) { ch.innerHTML = '<span class="sh-muted" style="font-size:0.8rem;">Belum cukup data (butuh minimal 2 siklus).</span>'; }
          else {
            var W = 700, H = 130, vals = cv.map(function (p) { return p.equity; }), base = t.start_equity || vals[0];
            var mn = Math.min.apply(null, vals.concat([base])), mx = Math.max.apply(null, vals.concat([base])); if (mx === mn) { mx += 1; mn -= 1; }
            var X = function (i) { return 10 + i * (W - 20) / (cv.length - 1); }, Y = function (v) { return 10 + (mx - v) * (H - 24) / (mx - mn); };
            var pts = cv.map(function (p, i) { return X(i).toFixed(1) + ',' + Y(p.equity).toFixed(1); }).join(' ');
            var up = vals[vals.length - 1] >= base;
            ch.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%; height:150px;"><line x1="10" x2="' + (W - 10) + '" y1="' + Y(base).toFixed(1) + '" y2="' + Y(base).toFixed(1) + '" stroke="#52525b" stroke-dasharray="4 4"/>' +
              '<polyline fill="none" stroke="' + (up ? '#34d399' : '#f87171') + '" stroke-width="2" points="' + pts + '"/>' +
              '<text x="12" y="12" fill="#a1a1aa" font-size="10">' + cv[0].ts + '</text><text x="' + (W - 12) + '" y="12" fill="#a1a1aa" font-size="10" text-anchor="end">' + cv[cv.length - 1].ts + ' &bull; ' + rp(vals[vals.length - 1]) + '</text></svg>';
          }
          // posisi
          var ps = d.positions || [];
          document.getElementById('shSimPos').innerHTML = ps.length ? '<table class="sh-t"><thead><tr><th>Saham</th><th>Lot</th><th>Avg</th><th>Kini</th><th>TP</th><th>CL</th><th>Nilai</th><th>P&amp;L</th></tr></thead><tbody>' +
            ps.map(function (p) { return '<tr title="' + esc(p.reason || '') + '"><td><b style="color:#fff;">' + esc(p.ticker) + '</b> <span class="sh-muted">' + esc(p.since) + '</span></td><td>' + p.lots + '</td><td>' + num(p.avg, nf1) + '</td><td>' + num(p.last) + '</td><td class="sh-up">' + num(p.tp) + '</td><td class="sh-down">' + num(p.cl) + '</td><td>' + rp(p.value) + '</td><td class="' + pnlc(p.pnl) + '">' + rp(p.pnl) + ' (' + (p.pnl_pct > 0 ? '+' : '') + p.pnl_pct + '%)</td></tr>'; }).join('') + '</tbody></table>'
            : '<div class="sh-muted" style="font-size:0.8rem; padding:0.5rem 0;">Tidak ada posisi (100% kas).</div>';
          var tr = d.trades || [];
          document.getElementById('shSimTrades').innerHTML = tr.length ? '<table class="sh-t"><thead><tr><th>Jam</th><th>Saham</th><th></th><th>Lot</th><th>Harga</th><th>Fee</th><th>P&amp;L</th></tr></thead><tbody>' +
            tr.map(function (x) { return '<tr title="' + esc(x.reason || '') + '"><td>' + esc(x.ts) + '</td><td><b style="color:#fff;">' + esc(x.ticker) + '</b></td><td class="' + (x.side === 'BUY' ? 'sh-up' : 'sh-down') + '">' + (x.side === 'BUY' ? 'BELI' : 'JUAL') + '</td><td>' + x.lots + '</td><td>' + num(x.price) + '</td><td>' + nf.format(x.fee) + '</td><td class="' + pnlc(x.pnl) + '">' + (x.pnl == null ? '-' : rp(x.pnl) + ' (' + (x.pnl_pct > 0 ? '+' : '') + x.pnl_pct + '%)') + '</td></tr>'; }).join('') + '</tbody></table>'
            : '<div class="sh-muted" style="font-size:0.8rem; padding:0.5rem 0;">Belum ada transaksi hari ini.</div>';
          var lg = d.log || [], kc = { BUY: 'sh-up', SELL: 'sh-down', AI: '', ERROR: 'sh-down', SKIP: 'sh-flat' };
          document.getElementById('shSimLog').innerHTML = lg.length ? lg.map(function (l) { return '<div style="padding:0.25rem 0; border-top:1px solid #1c1c22; font-size:0.76rem;"><span class="sh-muted">' + esc(l.ts) + '</span> <b class="' + (kc[l.kind] || '') + '">' + esc(l.kind) + '</b> ' + esc(l.text) + '</div>'; }).join('')
            : '<div class="sh-muted" style="font-size:0.8rem;">Belum ada log.</div>';
          shSimReport();

        }
        function rpRange() {
          var p = document.getElementById('shRpPeriod').value, to = null, from = null, today = new Date();
          var ymd = function (d) { return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
          document.getElementById('shRpCustom').style.display = p === 'custom' ? 'inline' : 'none';
          if (p === '7' || p === '30') { var d0 = new Date(today.getTime() - (parseInt(p, 10) - 1) * 86400000); from = ymd(d0); to = ymd(today); }
          else if (p === 'custom') { from = document.getElementById('shRpFrom').value || null; to = document.getElementById('shRpTo').value || null; }
          return { from: from, to: to };
        }
        function rpQuery() { var r = rpRange(), q = 'acct=' + (SH.simAcct || 'live'); if (r.from) q += '&from=' + r.from; if (r.to) q += '&to=' + r.to; return q; }

        function lineSvg(vals, labels, base, fmt) {
          var W = 700, H = 170, n = vals.length;
          if (n < 2) return '<div class="sh-muted" style="font-size:0.8rem; padding:0.6rem 0;">Butuh minimal 1 hari selesai untuk grafik.</div>';
          var mn = Math.min.apply(null, vals.concat([base])), mx = Math.max.apply(null, vals.concat([base])); if (mx === mn) { mx += 1; mn -= 1; }
          var X = function (i) { return 12 + i * (W - 24) / (n - 1); }, Y = function (v) { return 14 + (mx - v) * (H - 36) / (mx - mn); };
          var pts = vals.map(function (v, i) { return X(i).toFixed(1) + ',' + Y(v).toFixed(1); }).join(' ');
          var up = vals[n - 1] >= base;
          var dots = vals.map(function (v, i) { return '<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(v).toFixed(1) + '" r="3.2" fill="' + (v >= base ? '#34d399' : '#f87171') + '"><title>' + labels[i] + ': ' + fmt(v) + '</title></circle>'; }).join('');
          return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%; height:190px;"><line x1="12" x2="' + (W - 12) + '" y1="' + Y(base).toFixed(1) + '" y2="' + Y(base).toFixed(1) + '" stroke="#52525b" stroke-dasharray="4 4"/>' +
            '<polyline fill="none" stroke="' + (up ? '#34d399' : '#f87171') + '" stroke-width="2" points="' + pts + '"/>' + dots +
            '<text x="12" y="10" fill="#a1a1aa" font-size="10">' + labels[0] + '</text><text x="' + (W - 12) + '" y="10" fill="#a1a1aa" font-size="10" text-anchor="end">' + labels[n - 1] + ' &bull; ' + fmt(vals[n - 1]) + '</text>' +
            '<text x="12" y="' + (H - 6) + '" fill="#71717a" font-size="9">min ' + fmt(mn) + '</text><text x="' + (W - 12) + '" y="' + (H - 6) + '" fill="#71717a" font-size="9" text-anchor="end">maks ' + fmt(mx) + '</text></svg>';
        }
        function barSvg(vals, labels) {
          var W = 700, H = 170, n = vals.length;
          if (!n) return '<div class="sh-muted" style="font-size:0.8rem; padding:0.6rem 0;">Belum ada hari yang selesai.</div>';
          var mx = Math.max.apply(null, vals.map(Math.abs).concat([1])), zero = 14 + (H - 36) / 2, half = (H - 36) / 2;
          var bw = Math.min(40, (W - 24) / n * 0.7), step = (W - 24) / n;
          var bars = vals.map(function (v, i) { var h = Math.abs(v) / mx * half, x = 12 + i * step + (step - bw) / 2;
            return '<rect x="' + x.toFixed(1) + '" y="' + (v >= 0 ? zero - h : zero).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + Math.max(h, 1).toFixed(1) + '" rx="2" fill="' + (v >= 0 ? '#34d399' : '#f87171') + '"><title>' + labels[i] + ': ' + rp(v) + '</title></rect>'; }).join('');
          return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%; height:190px;"><line x1="12" x2="' + (W - 12) + '" y1="' + zero + '" y2="' + zero + '" stroke="#52525b"/>' + bars +
            '<text x="12" y="10" fill="#a1a1aa" font-size="10">' + labels[0] + '</text><text x="' + (W - 12) + '" y="10" fill="#a1a1aa" font-size="10" text-anchor="end">' + labels[n - 1] + '</text>' +
            '<text x="12" y="' + (H - 6) + '" fill="#71717a" font-size="9">&plusmn;' + rp(mx) + ' (skala)</text></svg>';
        }

        window.shSimReport = function () {
          api('sim/report?' + rpQuery()).then(function (d) {
            if (!d || !d.success) return;
            var m = d.summary, sr = d.series || [];
            var card = function (label, val, sub, cls) { return '<div class="sh-g" style="min-width:140px;"><span class="sh-muted" style="font-size:0.7rem;">' + label + '</span><b class="' + (cls || '') + '">' + val + '</b>' + (sub || '') + '</div>'; };
            document.getElementById('shRpSummary').innerHTML =
              card('Realized P&amp;L', rp(m.realized_pnl), '<div>' + pct(m.realized_pct) + '</div>', pnlc(m.realized_pnl)) +
              card('Transaksi jual', m.sells, '<div class="sh-muted" style="font-size:0.7rem;">dari ' + m.total_trades + ' transaksi</div>') +
              card('Win rate', m.win_rate + '%', '<div class="sh-muted" style="font-size:0.7rem;">rata-rata +' + nf.format(m.avg_win) + ' / ' + nf.format(m.avg_loss) + '</div>') +
              card('Profit factor', m.profit_factor == null ? '-' : m.profit_factor, '<div class="sh-muted" style="font-size:0.7rem;">&gt;1 = untung</div>') +
              card('Hari profit / loss', m.win_days + ' / ' + m.loss_days, '<div class="sh-muted" style="font-size:0.7rem;">rata-rata ' + (m.avg_daily_pct >= 0 ? '+' : '') + m.avg_daily_pct + '%/hari</div>') +
              card('Max drawdown', (m.max_drawdown_pct || 0) + '%', '<div class="sh-muted" style="font-size:0.7rem;">penurunan terdalam</div>', 'sh-down') +
              card('Hari terbaik', m.best_day ? rp(m.best_day.pnl) : '-', '<div class="sh-muted" style="font-size:0.7rem;">' + (m.best_day ? m.best_day.date : '') + '</div>', 'sh-up') +
              card('Hari terburuk', m.worst_day ? rp(m.worst_day.pnl) : '-', '<div class="sh-muted" style="font-size:0.7rem;">' + (m.worst_day ? m.worst_day.date : '') + '</div>', 'sh-down');
            // grafik hari ke hari
            var vals = [d.base], labs = ['awal'];
            sr.forEach(function (x) { vals.push(x.end); labs.push(x.date); });
            document.getElementById('shRpEq').innerHTML = lineSvg(vals, labs, d.base, rp);
            document.getElementById('shRpBars').innerHTML = barSvg(sr.map(function (x) { return x.pnl; }), sr.map(function (x) { return x.date; }));
            // tabel harian (terbaru dulu)
            var rows = sr.slice().reverse();
            document.getElementById('shSimHist').innerHTML = rows.length ? '<table class="sh-t"><thead><tr><th>Tanggal</th><th>Ekuitas awal</th><th>Ekuitas akhir</th><th>P&amp;L hari</th><th>%</th><th>P&amp;L kumulatif</th><th>Jual</th><th>Win/Loss</th><th>Status</th></tr></thead><tbody>' +
              rows.map(function (x) { return '<tr style="cursor:pointer;" onclick="shSimDay(\'' + x.date + '\')"><td>' + x.date + '</td><td>' + rp(x.start) + '</td><td>' + rp(x.end) + '</td><td class="' + pnlc(x.pnl) + '">' + rp(x.pnl) + '</td><td>' + pct(x.pnl_pct) + '</td><td class="' + pnlc(x.cum_pnl) + '">' + rp(x.cum_pnl) + ' (' + (x.cum_pct > 0 ? '+' : '') + x.cum_pct + '%)</td><td>' + x.trades + '</td><td>' + x.wins + ' / ' + x.losses + '</td><td>' + (x.status === 'done' ? 'selesai' : 'berjalan') + '</td></tr>'; }).join('') + '</tbody></table>'
              : '<div class="sh-muted" style="font-size:0.8rem;">Belum ada riwayat pada periode ini. Hasil harian muncul setelah siklus pertama.</div>';
            // per saham
            var pt = d.per_ticker || [];
            document.getElementById('shPerTicker').innerHTML = pt.length ? '<b style="color:#fff; font-size:0.85rem;">Realized per saham</b><div class="sh-scroll" style="max-height:30vh; margin-top:0.3rem;"><table class="sh-t"><thead><tr><th>Saham</th><th>Jual</th><th>Win</th><th>Realized P&amp;L</th></tr></thead><tbody>' +
              pt.map(function (e) { return '<tr><td><b style="color:#fff;">' + esc(e.ticker) + '</b></td><td>' + e.n + '</td><td>' + e.wins + ' (' + Math.round(e.wins / e.n * 100) + '%)</td><td class="' + pnlc(e.pnl) + '">' + rp(e.pnl) + '</td></tr>'; }).join('') + '</tbody></table></div>' : '';
          });
        };

        window.shSimDay = function (date) {
          api('sim/day?acct=' + (SH.simAcct || 'live') + '&date=' + date).then(function (d) {
            if (!d || !d.success) return;
            var cv = d.curve || [], day = d.day || {};
            var chart = lineSvg(cv.map(function (p) { return p.equity; }), cv.map(function (p) { return p.ts; }), day.start_equity || (cv[0] && cv[0].equity) || 0, rp);
            var tr = d.trades || [];
            document.getElementById('shSimDay').innerHTML = '<div class="sh-card" style="margin-bottom:0.9rem; border-color:rgba(56,189,248,0.4);"><div style="display:flex; justify-content:space-between;"><b style="color:#fff;">Detail ' + esc(date) + '</b><button class="hb-btn" onclick="document.getElementById(\'shSimDay\').innerHTML=\'\'">Tutup</button></div>' +
              '<div class="sh-muted" style="font-size:0.78rem; margin:0.3rem 0;">P&amp;L hari ini <b class="' + pnlc(day.pnl) + '">' + rp(day.pnl) + '</b> (' + (day.pnl_pct > 0 ? '+' : '') + (day.pnl_pct || 0).toFixed(2) + '%) &bull; ' + (day.wins || 0) + ' win / ' + (day.losses || 0) + ' loss</div>' + chart +
              (tr.length ? '<div class="sh-scroll" style="max-height:34vh;"><table class="sh-t"><thead><tr><th>Jam</th><th>Saham</th><th></th><th>Lot</th><th>Harga</th><th>Fee</th><th>P&amp;L</th><th>Alasan</th></tr></thead><tbody>' +
                tr.map(function (x) { return '<tr><td>' + esc(x.ts) + '</td><td><b style="color:#fff;">' + esc(x.ticker) + '</b></td><td class="' + (x.side === 'BUY' ? 'sh-up' : 'sh-down') + '">' + (x.side === 'BUY' ? 'BELI' : 'JUAL') + '</td><td>' + x.lots + '</td><td>' + num(x.price) + '</td><td>' + nf.format(x.fee) + '</td><td class="' + pnlc(x.pnl) + '">' + (x.pnl == null ? '-' : rp(x.pnl) + ' (' + (x.pnl_pct > 0 ? '+' : '') + x.pnl_pct + '%)') + '</td><td style="white-space:normal; max-width:420px;">' + esc((x.reason || '').slice(0, 140)) + '</td></tr>'; }).join('') + '</tbody></table></div>' : '<div class="sh-muted">Tidak ada transaksi.</div>') + '</div>';
            document.getElementById('shSimDay').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          });
        };

        function jrQuery(offset, limit) {
          var q = rpQuery() + '&offset=' + offset + '&limit=' + limit + '&order=' + document.getElementById('shJrOrder').value, tk = document.getElementById('shJrTicker').value.trim();
          return 'sim/trades?' + q + (tk ? '&ticker=' + encodeURIComponent(tk) : '');
        }
        window.shSimJournal = function (reset) {
          if (reset) SH.jrOffset = 0;
          api(jrQuery(SH.jrOffset || 0, 200)).then(function (d) {
            if (!d || !d.success) return;
            var rows = d.trades.map(function (x) { return '<tr><td>' + x.trade_date + '</td><td>' + esc(x.ts) + '</td><td><b style="color:#fff;">' + esc(x.ticker) + '</b></td><td class="' + (x.side === 'BUY' ? 'sh-up' : 'sh-down') + '">' + (x.side === 'BUY' ? 'BELI' : 'JUAL') + '</td><td>' + x.lots + '</td><td>' + num(x.price) + '</td><td>' + rp(x.amount) + '</td><td>' + nf.format(x.fee) + '</td><td class="' + pnlc(x.pnl) + '">' + (x.pnl == null ? '-' : rp(x.pnl) + ' (' + (x.pnl_pct > 0 ? '+' : '') + x.pnl_pct + '%)') + '</td><td style="white-space:normal; max-width:380px;">' + esc((x.reason || '').slice(0, 120)) + '</td></tr>'; }).join('');
            var box = document.getElementById('shJournal');
            if (!SH.jrOffset) box.innerHTML = '<table class="sh-t" id="shJrTbl"><thead><tr><th>Tanggal</th><th>Jam</th><th>Saham</th><th></th><th>Lot</th><th>Harga</th><th>Nilai</th><th>Fee</th><th>P&amp;L (realized)</th><th>Alasan</th></tr></thead><tbody>' + rows + '</tbody></table><div id="shJrMore"></div>';
            else document.querySelector('#shJrTbl tbody').insertAdjacentHTML('beforeend', rows);
            if (!d.total) box.innerHTML = '<div class="sh-muted" style="font-size:0.8rem; padding:0.5rem 0;">Belum ada transaksi pada periode/filter ini.</div>';
            SH.jrOffset = (SH.jrOffset || 0) + d.trades.length;
            var more = document.getElementById('shJrMore');
            if (more) more.innerHTML = SH.jrOffset < d.total ? '<div style="text-align:center; padding:0.5rem;"><button class="hb-btn" onclick="shSimJournal(false)">Muat lebih banyak (' + (d.total - SH.jrOffset) + ' lagi)</button></div>' : '<div class="sh-muted" style="font-size:0.72rem; padding:0.4rem;">' + d.total + ' transaksi ditampilkan.</div>';
          });
        };
        window.shSimCsv = function () {
          api(jrQuery(0, 1000)).then(function (d) {
            if (!d || !d.success) return;
            var cols = ['trade_date', 'ts', 'ticker', 'side', 'lots', 'price', 'amount', 'fee', 'pnl', 'pnl_pct', 'reason'];
            var q = function (v) { v = (v == null ? '' : String(v)); return '"' + v.split('"').join('""') + '"'; };
            var csv = cols.join(',') + String.fromCharCode(10) + d.trades.map(function (x) { return cols.map(function (c) { return q(x[c]); }).join(','); }).join(String.fromCharCode(10));
            var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.download = 'simulasi-trading-' + (SH.simAcct || 'live') + '.csv'; a.click();
          });
        };

        window.shSimSave = function () {
          var body = { acct: SH.simAcct || 'live', capital: +document.getElementById('shSimCap').value, max_positions: +document.getElementById('shSimMaxPos').value, max_pos_pct: +document.getElementById('shSimMaxPct').value,
            fee_buy: +document.getElementById('shSimFeeB').value, fee_sell: +document.getElementById('shSimFeeS').value, enabled: document.getElementById('shSimEnabled').checked ? 1 : 0 };
          api('sim/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(function (d) { alert(d.success ? 'Pengaturan disimpan. (Modal baru berlaku setelah Reset akun.)' : (d.error || 'Gagal')); shSimLoad(true); });
        };
        window.shSimReset = function () {
          var cap = +document.getElementById('shSimCap').value;
          if (!confirm('Reset akun ' + (SH.simAcct || 'live') + ' dan mulai dari modal Rp' + nf.format(cap) + '? Semua posisi, transaksi, dan riwayat akun ini dihapus.')) return;
          api('sim/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ acct: SH.simAcct || 'live', capital: cap, confirm: 'yes' }) }).then(function (d) { if (!d.success) alert(d.error || 'Gagal'); shSimLoad(true); });
        };
        window.shSimRun = function (kind) {
          api('sim/' + kind, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ acct: SH.simAcct || 'live' }) }).then(function (d) {
            if (!d.success) alert(d.error || 'Gagal'); else { var n = 0, iv = setInterval(function () { shSimLoad(false); if (++n > 40) clearInterval(iv); }, 4000); }
          });
        };

        window.shClose = function () { document.getElementById('shModal').classList.remove('open'); };

        window.shDetail = function (mk, rank) {
          var m = SH.picks.methods.filter(function (x) { return x.key === mk; })[0];
          var p = m.picks.filter(function (x) { return x.rank === rank; })[0];
          var dt = p.data || {};
          var kv = [['Harga acuan', num(p.ref_price)], ['Perubahan', num(dt.chg, nf1) + '%'], ['Nilai transaksi', big(dt.value)], ['Kapitalisasi', big(dt.mcap)],
            ['RSI', num(dt.rsi, nf1)], ['ADX', num(dt.adx, nf1)], ['Volume relatif', num(dt.relvol, nf1) + 'x'], ['PER', num(dt.pe, nf1)], ['PBV', num(dt.pbv, nf1)], ['ROE', num(dt.roe, nf1) + '%'],
            ['DER', num(dt.der, nf1)], ['Dividen', num(dt.dy, nf1) + '%'], ['SMA20', num(dt.sma20)], ['SMA50', num(dt.sma50)], ['SMA200', num(dt.sma200)], ['High 52w', num(dt.h52)], ['Low 52w', num(dt.l52)], ['Perf 1B', num(dt.p1m, nf1) + '%']];
          var rr = (p.tp && p.cl && p.ref_price) ? ((p.tp - p.ref_price) / (p.ref_price - p.cl)) : null;
          var res = '<div style="margin:0.5rem 0; font-size:0.85rem;">' + lvl(p) + (rr ? ' <span class="sh-muted">&bull; risk:reward 1:' + rr.toFixed(2) + '</span>' : '') + '</div>' +
            (isEval(p) ? '<div style="margin:0.4rem 0; font-size:0.85rem;">' + outBadge(p) + (p.status === 'live' ? ' Terakhir <b>' : ' Penutupan <b>') + num(p.result_close) + '</b> ' + pct(p.result_ret) +
              ' <span class="sh-muted">&bull; high ' + num(p.day_high) + ' / low ' + num(p.day_low) + '</span><div class="sh-muted" style="font-size:0.75rem;">' + esc(p.note || '') + '</div></div>' : '');
          document.getElementById('shModalBody').innerHTML =
            '<div style="display:flex; justify-content:space-between; gap:1rem;"><div><div style="font-size:1.15rem; font-weight:700; color:#fff;">' + sentDot(p.sentiment) + esc(p.ticker) + '</div><div class="sh-muted" style="font-size:0.78rem;">' + esc(p.name) + ' &bull; ' + esc(p.sector) + ' &bull; metode: ' + esc(m.label) + '</div></div><button class="hb-btn" onclick="shClose()">Tutup</button></div>' +
            '<div style="font-size:0.82rem; color:#d4d4d8; margin-top:0.5rem;">' + esc(p.reason) + '</div>' + res +
            '<div class="sh-kv">' + kv.map(function (x) { return '<div>' + x[0] + '<b>' + x[1] + '</b></div>'; }).join('') + '</div>' +
            '<div class="sh-card" style="margin-bottom:0.7rem;"><b style="color:#fff; font-size:0.85rem;">&#129302; Analisis AI</b>' +
            '<div style="font-size:0.82rem; color:#d4d4d8; margin-top:0.3rem;">' + esc(p.ai_summary || 'Belum ada analisis AI untuk saham ini.') + '</div>' +
            (p.catalyst && p.catalyst !== '-' ? '<div style="font-size:0.78rem; margin-top:0.3rem;"><b class="sh-up">Katalis:</b> ' + esc(p.catalyst) + '</div>' : '') +
            (p.risk && p.risk !== '-' ? '<div style="font-size:0.78rem; margin-top:0.2rem;"><b class="sh-down">Risiko:</b> ' + esc(p.risk) + '</div>' : '') + '</div>' +
            '<b style="color:#fff; font-size:0.85rem;">&#128240; Berita terkait</b><div id="shNews" class="sh-muted" style="font-size:0.8rem; margin-top:0.3rem;">Memuat berita...</div>';
          document.getElementById('shModal').classList.add('open');
          api('news?ticker=' + encodeURIComponent(p.ticker) + '&date=' + encodeURIComponent(SH.picks.date)).then(function (n) {
            var box = document.getElementById('shNews');
            if (!n.news || !n.news.length) { box.textContent = 'Tidak ada berita spesifik untuk saham ini.'; return; }
            box.innerHTML = n.news.map(function (x) { return '<div style="padding:0.3rem 0; border-top:1px solid #1c1c22;"><a class="sh-link" target="_blank" rel="noopener noreferrer" href="' + esc(x.link) + '">' + esc(x.title) + '</a><div class="sh-muted" style="font-size:0.68rem;">' + esc(x.source) + ' &bull; ' + esc(x.published) + '</div></div>'; }).join('');
          });
        };

        window.shLoadStats = function () {
          api('stats?window=' + document.getElementById('shWin').value).then(function (d) {
            var rows = (d.methods || []).map(function (m) {
              var isBest = d.best === m.method;
              return '<tr><td><b style="color:#fff;">' + esc(m.label) + '</b>' + (isBest ? '<span class="sh-badge">PALING AKURAT</span>' : '') + '</td><td>' + m.n + '</td><td>' + m.days + '</td>' +
                '<td><div style="display:flex; align-items:center; gap:0.4rem;"><div class="sh-bar"><i style="width:' + Math.min(100, m.tp_rate) + '%"></i></div>' + (m.n ? m.tp_rate.toFixed(1) + '%' : '-') + '</div></td>' +
                '<td class="sh-down">' + (m.n ? m.cl_rate.toFixed(1) + '%' : '-') + '</td><td>' + (m.n ? m.hit_rate.toFixed(1) + '%' : '-') + '</td>' +
                '<td>' + (m.n ? pct(m.avg_ret) : '-') + '</td><td>' + (m.n ? pct(m.avg_alpha) : '-') + '</td><td>' + (m.n ? m.beat_rate.toFixed(1) + '%' : '-') + '</td>' +
                '<td>' + (m.best ? esc(m.best[0]) + ' ' + pct(m.best[1]) : '-') + '</td><td>' + (m.worst ? esc(m.worst[0]) + ' ' + pct(m.worst[1]) : '-') + '</td></tr>';
            }).join('');
            var any = (d.methods || []).some(function (m) { return m.n > 0; });
            document.getElementById('shAcc').innerHTML = '<div class="sh-card sh-scroll"><table class="sh-t"><thead><tr><th>Metode</th><th>Rekomendasi</th><th>Hari</th><th>TP tercapai</th><th>CL</th><th>Naik (close)</th><th>Rata-rata return</th><th>Alpha vs pasar</th><th>Kalahkan pasar</th><th>Terbaik</th><th>Terburuk</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
              (any ? '<div class="sh-muted" style="font-size:0.72rem; margin-top:0.4rem;">TP tercapai = high hari itu menyentuh target; CL = low menyentuh cut loss (bila keduanya tersentuh dihitung CL, karena urutan tidak diketahui); Naik (close) = ditutup di atas harga acuan. Peringkat memakai selisih TP% &minus; CL%. Alpha = return rekomendasi dikurangi rata-rata return semua saham likuid hari itu. &ldquo;Paling akurat&rdquo; butuh minimal 10 rekomendasi tereval.</div>' :
                '<div class="sh-muted" style="font-size:0.8rem; margin-top:0.6rem;">Belum ada data evaluasi. Statistik terisi setelah analisis pra-pasar 08:20 dan evaluasi 16:30 berjalan minimal satu hari bursa.</div>');
            var methods = (d.methods || []).map(function (m) { return m; });
            var head = '<tr><th>Tanggal</th><th>IHSG</th><th>Pasar</th>' + methods.map(function (m) { return '<th title="' + esc(m.label) + '">' + esc(m.label.split(' ')[0]) + '</th>'; }).join('') + '</tr>';
            var body = (d.history || []).map(function (h) {
              if (h.status !== 'done') return '<tr><td>' + h.date + '</td><td colspan="' + (methods.length + 2) + '" class="sh-muted">' + (h.status === 'no_session' ? 'Bursa libur / tidak ada sesi' : h.status) + '</td></tr>';
              return '<tr><td>' + h.date + '</td><td>' + pct(h.ihsg_ret) + '</td><td>' + pct(h.bench_ret) + '</td>' + methods.map(function (m) {
                var x = h.methods[m.method]; if (!x) return '<td class="sh-flat">-</td>';
                var c = x.hit_rate >= 60 ? 'sh-up' : (x.hit_rate < 40 ? 'sh-down' : 'sh-flat'); return '<td class="' + c + '">' + x.hit_rate.toFixed(0) + '%</td>';
              }).join('') + '</tr>';
            }).join('') || '<tr><td class="sh-muted">Belum ada riwayat.</td></tr>';
            document.getElementById('shHist').innerHTML = '<table class="sh-t"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
          });
        };

        window.shLoadAll = function (live) {
          document.getElementById('shAllNote').textContent = 'Memuat...';
          api('stocks' + (live ? '?live=1' : '')).then(function (d) {
            if (!d.success) { document.getElementById('shAllNote').textContent = d.error || 'Gagal'; return; }
            SH.all = d.stocks;
            var sectors = {}; d.stocks.forEach(function (s) { if (s.sector) sectors[s.sector] = 1; });
            var sel = document.getElementById('shSector'), cur = sel.value;
            sel.innerHTML = '<option value="">Semua sektor</option>' + Object.keys(sectors).sort().map(function (s) { return '<option>' + esc(s) + '</option>'; }).join('');
            sel.value = cur;
            document.getElementById('shAllNote').innerHTML = d.count + ' saham &bull; diambil ' + d.fetched + ' WIB (tertunda 10 mnt)';
            SH.allShown = 150; shRenderAll();
          }).catch(function (e) { document.getElementById('shAllNote').textContent = 'Gagal memuat: ' + e; });
        };

        var COLS = [['ticker', 'Kode'], ['name', 'Nama'], ['sector', 'Sektor'], ['close', 'Harga'], ['chg', 'Chg%'], ['vol', 'Volume'], ['value', 'Nilai'], ['mcap', 'Mkt Cap'], ['pe', 'PER'], ['pbv', 'PBV'],
          ['roe', 'ROE'], ['dy', 'Div%'], ['rsi', 'RSI'], ['adx', 'ADX'], ['relvol', 'RelVol'], ['rec', 'Skor TV'], ['p1m', '1B%'], ['p3m', '3B%']];

        window.shSort = function (k) { if (SH.sortKey === k) SH.sortDir *= -1; else { SH.sortKey = k; SH.sortDir = (k === 'ticker' || k === 'name' || k === 'sector') ? 1 : -1; } shRenderAll(); };
        window.shMore = function () { SH.allShown += 300; shRenderAll(); };

        window.shRenderAll = function () {
          var q = document.getElementById('shQ').value.trim().toLowerCase(), sec = document.getElementById('shSector').value, liq = document.getElementById('shLiq').checked;
          var rows = SH.all.map(function (s) { s.value = (s.close || 0) * (s.vol || 0); return s; }).filter(function (s) {
            if (q && (s.ticker || '').toLowerCase().indexOf(q) < 0 && (s.desc || '').toLowerCase().indexOf(q) < 0) return false;
            if (sec && s.sector !== sec) return false;
            if (liq && !(s.close >= 50 && s.value >= 2e9 && s.mcap >= 5e11)) return false;
            return true;
          });
          var k = SH.sortKey, dir = SH.sortDir;
          rows.sort(function (a, b) { var x = a[k], y = b[k]; if (x == null) return 1; if (y == null) return -1; return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir; });
          var shown = rows.slice(0, SH.allShown);
          var head = '<thead><tr>' + COLS.map(function (c) { return '<th onclick="shSort(\'' + c[0] + '\')">' + c[1] + (SH.sortKey === c[0] ? (SH.sortDir > 0 ? ' &#9650;' : ' &#9660;') : '') + '</th>'; }).join('') + '</tr></thead>';
          var body = shown.map(function (s) {
            return '<tr><td><b style="color:#fff;">' + esc(s.ticker) + '</b></td><td>' + esc((s.desc || '').replace(/^PT\.?\s+/i, '').slice(0, 28)) + '</td><td>' + esc(s.sector || '') + '</td><td>' + num(s.close) + '</td><td>' + pct(s.chg) + '</td><td>' + big(s.vol) + '</td><td>' + big(s.value) + '</td><td>' + big(s.mcap) + '</td>' +
              '<td>' + num(s.pe, nf1) + '</td><td>' + num(s.pbv, nf1) + '</td><td>' + num(s.roe, nf1) + '</td><td>' + num(s.dy, nf1) + '</td><td>' + num(s.rsi, nf1) + '</td><td>' + num(s.adx, nf1) + '</td><td>' + num(s.relvol, nf1) + '</td><td>' + num(s.rec == null ? null : s.rec * 100, nf) + '</td><td>' + pct(s.p1m) + '</td><td>' + pct(s.p3m) + '</td></tr>';
          }).join('');
          document.getElementById('shAllTbl').innerHTML = head + '<tbody>' + body + '</tbody>' + (rows.length > shown.length ? '<tfoot><tr><td colspan="18" style="text-align:center; padding:0.6rem;"><button class="hb-btn" onclick="shMore()">Tampilkan lebih banyak (' + (rows.length - shown.length) + ' lagi)</button></td></tr></tfoot>' : '');
        };
      })();
      </script>
    </div>
"""
