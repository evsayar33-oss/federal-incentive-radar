"""H083 forward-only monthly paper experiment and conditional information plans.

No broker API. Cannot seed validation with H083/H084 historical trades.
A decision is frozen at an observed NYSE month-end close; the following
month's entry and next month's exit are observed from their respective opens.
If a scheduled run misses a fill date, it is invalid (NEVER reconstructed).
"""
from __future__ import annotations

import html
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy import stats

from . import event_watch as W
from . import h083_catalog as C
from . import store
from .settings import store_dir

LOG = logging.getLogger("radar.h083_forward")
RULES = W.RULES
CAPITAL = 10000.0
TICKET = 1990.0
COMMISSION = 1.50
SLIP_BPS = 5.0
STRESS_BPS = 30.0
MIN_MONTHS = 24
MIN_POSITIONS = 60
MAX_MONTHS = 36
TIMEZONE = ZoneInfo("America/New_York")
COHORT_COLS = ["cohort_id","rule","entry_month","published_at_utc","signal_session",
               "entry_session","exit_session","tickers_json","events_json","n_candidates",
               "regime","study_version"]
FILL_COLS = ["cohort_id","phase","session","ticker","open_usd","valid","issue","observed_at_utc"]
RESULT_COLS = ["cohort_id","rule","entry_month","signal_session","entry_session","exit_session",
               "n_positions","net_return","spy_net_return","excess_vs_spy",
               "net_return_stress","complete","failure","closed_at_utc"]
GATE_COLS = ["rule","status","closed_months","paper_positions","mean_monthly_excess",
             "total_strategy_return","total_spy_return","pretest_p","bh_q",
             "bootstrap_95_lower","stress_total_return","validated_for_info_plans",
             "evaluated_at_utc","explanation"]
PLAN_COLS = ["plan_id","rule","ticker","side","target_session","exit_session",
             "max_notional_usd","indicative_last_close","estimated_whole_shares",
             "event_family","event_category","source_url","signal_session",
             "status","expected_order_status","commission_usd","slippage_bps",
             "notes","created_at_utc"]


def _read(name: str, columns: list[str]) -> pd.DataFrame:
    current = store.read(name)
    if current is None or current.empty:
        return pd.DataFrame(columns=columns)
    if not set(columns).issubset(current):
        raise ValueError(f"Corrupt {name}: missing {sorted(set(columns)-set(current))}")
    return current.copy()


def _calendar(now: datetime):
    """Return NYSE sessions around today from the canonical exchange calendar."""
    import pandas_market_calendars as mcal
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is None:
        raise ValueError("Expected an aware timestamp")
    et = stamp.tz_convert("America/New_York")
    today = et.normalize().tz_localize(None)
    sch = mcal.get_calendar("NYSE").schedule(
        start_date=(today - pd.Timedelta(days=8)).date(),
        end_date=(today + pd.Timedelta(days=110)).date(),
    )
    idx = pd.DatetimeIndex(sch.index).tz_localize(None).normalize()
    return today, sch, idx


def _active_session(now: datetime, today, schedule) -> tuple[bool,str]:
    if today not in schedule.index:
        return False,"NYSE_CLOSED"
    et=pd.Timestamp(now).tz_convert("America/New_York")
    # Both UTC cron expressions are installed for DST. Ignore the wrong slot.
    # A delayed out-of-window run must not backdate an entry decision.
    if et.hour != 17:
        return False,"OUTSIDE_17_ET_AFTERCLOSE_SLOT"
    official_close = pd.Timestamp(schedule.loc[today,"market_close"])
    if pd.Timestamp(now).tz_convert("UTC") < official_close + pd.Timedelta(minutes=45):
        return False,"WAIT_FOR_FINAL_OHLCV"
    return True,"AFTER_NYSE_CLOSE"


def _month_end_rebalance(today, index):
    """Only the actual last NYSE session may publish next-month orders."""
    ix=index.get_loc(today)
    if ix+1>=len(index):
        raise ValueError("Trading calendar truncated")
    first=index[ix+1]
    if first.month==today.month:
        return None
    later=[d for d in index[ix+2:] if (d.year,d.month)!=(first.year,first.month)]
    if not later:
        raise ValueError("No next month exit session")
    return first,later[0]


def _parse_members(row: dict) -> list[str]:
    symbols=json.loads(row["tickers_json"])
    if not isinstance(symbols,list) or len(symbols)>5 or len(symbols)!=len(set(symbols)):
        raise ValueError("Corrupt frozen cohort")
    if any(not W.TICKER_RE.fullmatch(str(t)) for t in symbols):
        raise ValueError("Unsafe ticker in cohort")
    return symbols


def _pending_tickers(cohorts: pd.DataFrame, today) -> list[str]:
    if cohorts.empty:
        return []
    active = cohorts[pd.to_datetime(cohorts.exit_session) >= today-pd.Timedelta(days=2)]
    names = set()
    for row in active.to_dict("records"):
        names.update(_parse_members(row))
    return sorted(names)


def _closed_ohlcv_open(bars: dict[str,pd.DataFrame],ticker: str,day) -> float | None:
    p=W._regular_bars(bars.get(ticker,pd.DataFrame()))
    d=pd.Timestamp(day).normalize()
    if d not in p.index:
        return None
    v=p.loc[d,"Open"]
    return float(v) if pd.notna(v) and math.isfinite(float(v)) and float(v)>0 else None


def freeze_cohorts(
    today, index: pd.DatetimeIndex, watch: pd.DataFrame,
    cohorts: pd.DataFrame, now: datetime,
) -> tuple[pd.DataFrame,pd.DataFrame]:
    """No backfill. Persist even empty selection to measure the cash alternative."""
    next_sessions=_month_end_rebalance(today,index)
    if next_sessions is None:
        return cohorts,pd.DataFrame(columns=COHORT_COLS)
    entry,exit_day=next_sessions
    fresh=[]
    now_iso=pd.Timestamp(now).tz_convert("UTC").isoformat()
    for rule in RULES:
        key=f"H083v1|{rule}|{entry.strftime('%Y-%m')}"
        if not cohorts.empty and key in set(cohorts.cohort_id.astype(str)):
            continue
        chosen=watch[watch.rule.eq(rule)].sort_values(["rank","ticker"]).head(5) if not watch.empty else watch
        if not chosen.empty and not chosen.feature_asof.eq(str(today.date())).all():
            raise ValueError("Candidate lookahead/staleness: expected completed close")
        symbols=chosen.ticker.astype(str).tolist() if not chosen.empty else []
        events=[{"ticker":str(r["ticker"]),"event_id":str(r.get("event_id","")),
                 "event_family":str(r.get("event_family","")),"category":str(r.get("category","")),
                 "source_url":str(r.get("source_url",""))}
                for r in chosen.to_dict("records")]
        fresh.append({
            "cohort_id":key,"rule":rule,"entry_month":entry.strftime("%Y-%m"),
            "published_at_utc":now_iso,"signal_session":str(today.date()),
            "entry_session":str(entry.date()),"exit_session":str(exit_day.date()),
            "tickers_json":json.dumps(symbols),"events_json":json.dumps(events,ensure_ascii=False),
            "n_candidates":len(symbols),"regime":str(chosen.iloc[0].market_regime) if len(chosen) else "UNKNOWN",
            "study_version":"H083v1_FORWARD_SINCE_DEPLOY",
        })
    new=pd.DataFrame(fresh,columns=COHORT_COLS)
    combined=pd.concat([cohorts,new],ignore_index=True)
    return combined,new


def capture_fills(today, now: datetime, cohorts: pd.DataFrame, bars: dict[str,pd.DataFrame],
                  existing: pd.DataFrame) -> pd.DataFrame:
    """Capture daily OHLCV open only on its actual NYSE session, never on later days."""
    if cohorts.empty:
        return existing
    rows=[]
    already={(str(r.cohort_id),str(r.phase),str(r.ticker))
             for r in existing.itertuples(index=False)} if not existing.empty else set()
    now_iso=pd.Timestamp(now).tz_convert("UTC").isoformat()
    for r in cohorts.to_dict("records"):
        for phase,col in (("ENTRY","entry_session"),("EXIT","exit_session")):
            day=pd.Timestamp(r[col])
            if day>today:
                continue
            symbols=_parse_members(r)+["SPY"]
            for ticker in symbols:
                key=(r["cohort_id"],phase,ticker)
                if key in already:
                    continue
                if day!=today:
                    # This run missed the actual entry/exit; never retroactively
                    # reconstruct the open and pretend it was observed live.
                    value=None;issue="MISSED_ACTUAL_SESSION"
                else:
                    value=_closed_ohlcv_open(bars,ticker,day)
                    issue="" if value is not None else "OHLCV_OPEN_MISSING_ON_SESSION"
                rows.append({"cohort_id":r["cohort_id"],"phase":phase,
                             "session":str(day.date()),"ticker":ticker,
                             "open_usd":value if value is not None else np.nan,
                             "valid":value is not None,"issue":issue,
                             "observed_at_utc":now_iso})
                already.add(key)
    if rows:
        return pd.concat([existing,pd.DataFrame(rows,columns=FILL_COLS)],ignore_index=True)
    return existing


def _net_month(entry: dict,exit_prices: dict,assets: list[str], slippage_bps: float):
    slippage=slippage_bps/10000.
    cash=CAPITAL
    for ticker in assets:
        buy=entry[ticker]
        sale=exit_prices[ticker]
        shares=TICKET/(buy*(1+slippage))
        cash-=TICKET+COMMISSION
        cash+=shares*sale*(1-slippage)-COMMISSION
    return cash/CAPITAL-1.


def resolve_months(now: datetime, today, cohorts: pd.DataFrame,
                   fills: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    """Permanently freeze outcomes; never change later on Yahoo revisions."""
    if cohorts.empty:
        return old
    done=set(old.cohort_id.astype(str)) if not old.empty else set()
    additions=[]
    now_iso=pd.Timestamp(now).tz_convert("UTC").isoformat()
    for r in cohorts.to_dict("records"):
        if r["cohort_id"] in done or pd.Timestamp(r["exit_session"])>today:
            continue
        members=_parse_members(r)
        rows=fills[fills.cohort_id.eq(r["cohort_id"])]
        expected={(phase,ticker) for phase in ("ENTRY","EXIT") for ticker in members+["SPY"]}
        got={(str(x.phase),str(x.ticker)) for x in rows.itertuples(index=False)}
        complete=got==expected and not rows.empty and rows.valid.astype(str).str.lower().isin(["true","1"]).all()
        pnl=bench=stress=np.nan
        if complete:
            vals={(str(x.phase),str(x.ticker)):float(x.open_usd) for x in rows.itertuples(index=False)}
            ent={t:vals[("ENTRY",t)] for t in members+["SPY"]}
            ex={t:vals[("EXIT",t)] for t in members+["SPY"]}
            pnl=_net_month(ent,ex,members,SLIP_BPS)
            stress=_net_month(ent,ex,members,STRESS_BPS)
            # Fully-invested SPY benchmark: same account, one fee each side.
            share=(CAPITAL-COMMISSION)/(ent["SPY"]*(1+SLIP_BPS/10000))
            bench=(share*ex["SPY"]*(1-SLIP_BPS/10000)-COMMISSION)/CAPITAL-1
        additions.append({
            "cohort_id":r["cohort_id"],"rule":r["rule"],
            "entry_month":r["entry_month"],"signal_session":r["signal_session"],
            "entry_session":r["entry_session"],"exit_session":r["exit_session"],
            "n_positions":len(members),"net_return":pnl,"spy_net_return":bench,
            "excess_vs_spy":pnl-bench if complete else np.nan,
            "net_return_stress":stress,"complete":bool(complete),
            "failure":"" if complete else "MISSING_OR_UNVERIFIED_ON_DAY_FILL",
            "closed_at_utc":now_iso,
        })
        done.add(r["cohort_id"])
    if not additions:
        return old
    return pd.concat([old,pd.DataFrame(additions,columns=RESULT_COLS)],ignore_index=True)


def _bh(pvalues: list[float]) -> list[float]:
    p=np.asarray(pvalues,dtype=float)
    order=np.argsort(p)
    s=np.minimum.accumulate((p[order]*len(p)/np.arange(1,len(p)+1))[::-1])[::-1]
    out=np.empty_like(s)
    out[order]=np.clip(s,0,1)
    return out.tolist()


def _bootstrap_lower(x: np.ndarray) -> float:
    """3-month moving block bootstrap, fixed seed; approximate CI, not absolute proof."""
    rng=np.random.default_rng(83)
    n=len(x)
    if n<3:
        return float("nan")
    starts=rng.integers(0,n-2,size=(2000,math.ceil(n/3)))
    inds=np.concatenate([starts+i for i in range(3)],axis=1)[:,:n]
    return float(np.quantile(x[inds].mean(axis=1),.05))


def evidence_gate(results: pd.DataFrame,now: datetime) -> pd.DataFrame:
    """Strict forward-only, two-family BH. Never imports historical H083 PnL."""
    items=[]
    pre_p=[]
    for rule in RULES:
        part=results[results.rule.eq(rule)].sort_values("entry_month") if not results.empty else pd.DataFrame(columns=RESULT_COLS)
        # Gate uses most recent MAX_MONTHS closed months, including INVALID months.
        part=part.tail(MAX_MONTHS)
        valid=part[part.complete.astype(str).str.lower().isin(["true","1"])].copy()
        xs=pd.to_numeric(valid.excess_vs_spy,errors="coerce").to_numpy(dtype=float)
        ys=pd.to_numeric(valid.net_return,errors="coerce").to_numpy(dtype=float)
        bs=pd.to_numeric(valid.spy_net_return,errors="coerce").to_numpy(dtype=float)
        stress=pd.to_numeric(valid.net_return_stress,errors="coerce").to_numpy(dtype=float)
        p=1.
        lower=float("nan")
        if len(xs)>=MIN_MONTHS and np.isfinite(xs).all() and xs.std(ddof=1)>1e-6:
            p=float(stats.ttest_1samp(xs,0,alternative="greater").pvalue)
            lower=_bootstrap_lower(xs)
        consecutive=(len(part)>=MIN_MONTHS and len(valid)==len(part) and
                     len(valid.entry_month.unique())==len(valid) and
                     len(valid)>=MIN_MONTHS)
        if consecutive:
            periods=pd.PeriodIndex(valid.entry_month,freq="M")
            consecutive=all((b.ordinal-a.ordinal)==1 for a,b in zip(periods[:-1],periods[1:]))
        positions=int(pd.to_numeric(valid.n_positions,errors="coerce").fillna(0).sum())
        total=float(np.prod(1+ys)-1) if len(ys) else np.nan
        benchmark=float(np.prod(1+bs)-1) if len(bs) else np.nan
        stressed=float(np.prod(1+stress)-1) if len(stress) else np.nan
        items.append({
            "rule":rule,"closed_months":len(valid),"paper_positions":positions,
            "mean_monthly_excess":float(xs.mean()) if len(xs) else np.nan,
            "total_strategy_return":total,"total_spy_return":benchmark,
            "pretest_p":p,"bootstrap_95_lower":lower,
            "stress_total_return":stressed,"consecutive":consecutive,
        })
        pre_p.append(p)
    q=_bh(pre_p)
    now_iso=pd.Timestamp(now).tz_convert("UTC").isoformat()
    rows=[]
    for x,qv in zip(items,q):
        pass_gate=(x["consecutive"] and x["closed_months"]>=MIN_MONTHS and
                   x["paper_positions"]>=MIN_POSITIONS and qv<=0.05 and
                   np.isfinite(x["bootstrap_95_lower"]) and
                   x["bootstrap_95_lower"]>0 and
                   x["total_strategy_return"]>x["total_spy_return"] and
                   x["total_strategy_return"]>0 and
                   x["stress_total_return"]>0)
        status=("FORWARD_VALIDATED_PAPER" if pass_gate else
                "COLLECTING_FORWARD" if x["closed_months"]<MIN_MONTHS
                else "NOT_STATISTICALLY_VALIDATED")
        rows.append({k:v for k,v in x.items() if k!="consecutive"}|{
            "status":status,"bh_q":qv,"validated_for_info_plans":bool(pass_gate),
            "evaluated_at_utc":now_iso,
            "explanation":(
                "İleriye dönük kağıt simülasyonda katı maliyet/çoklu test kapıları geçti; gerçek emir kanıtı değil."
                if pass_gate else
                "En az 24 ardışık tam ay, 60 işlem, SPY üstü net ve stres getirisi, "
                "BH q<=0,05 ve pozitif 3-ay blok bootstrap sınırı gerekli."
            )})
    return pd.DataFrame(rows,columns=GATE_COLS)


def create_plans(today, now: datetime, cohorts_new: pd.DataFrame,
                 watch: pd.DataFrame, bars: dict, gate: pd.DataFrame,
                 old_plans: pd.DataFrame) -> pd.DataFrame:
    """Only gate-approved months produce publicly viewable information plans."""
    plans=[]
    timestamp=pd.Timestamp(now).tz_convert("UTC").isoformat()
    for co in cohorts_new.to_dict("records"):
        r=gate[gate.rule.eq(co["rule"])]
        if r.empty or not bool(r.iloc[0].validated_for_info_plans):
            continue
        selection=watch[watch.rule.eq(co["rule"])]
        for row in selection.to_dict("records"):
            ticker=str(row["ticker"])
            close_frame=W._regular_bars(bars.get(ticker,pd.DataFrame()))
            if today not in close_frame.index:
                continue
            mark=float(close_frame.loc[today,"Close"])
            if not math.isfinite(mark) or mark<=0:
                continue
            lot=math.floor((TICKET-COMMISSION)/(mark*(1+SLIP_BPS/10000)))
            for side,target in (("BUY_NEXT_OPEN",co["entry_session"]),
                                ("EXIT_NEXT_MONTH_OPEN",co["exit_session"])):
                plans.append({
                    "plan_id":f"{co['cohort_id']}|{side}|{ticker}",
                    "rule":co["rule"],"ticker":ticker,"side":side,
                    "target_session":target,"exit_session":co["exit_session"],
                    "max_notional_usd":TICKET if side=="BUY_NEXT_OPEN" else np.nan,
                    "indicative_last_close":mark,
                    "estimated_whole_shares":lot if side=="BUY_NEXT_OPEN" else np.nan,
                    "event_family":str(row.get("event_family","")),
                    "event_category":str(row.get("category","")),
                    "source_url":str(row.get("source_url","")),
                    "signal_session":co["signal_session"],
                    "status":"FORWARD_VALIDATED_PAPER",
                    "expected_order_status":"INFORMATION_ONLY_NO_BROKER_EXECUTION",
                    "commission_usd":COMMISSION,
                    "slippage_bps":SLIP_BPS,
                    "notes":"Beklenen seans AÇILIŞ fiyatı bilinmiyor; kapanış yalnız yaklaşık ölçüdür. "
                            "ATR stop/hedef bu aylık rotasyon kuralında yoktur.",
                    "created_at_utc":timestamp,
                })
    if not plans:
        return old_plans
    out=pd.concat([old_plans,pd.DataFrame(plans,columns=PLAN_COLS)],ignore_index=True)
    return out.drop_duplicates("plan_id",keep="first")


def format_monthly_telegram(new: pd.DataFrame, gate: pd.DataFrame,
                            plans: pd.DataFrame) -> str:
    esc=html.escape
    lines=["<b>🔬 H083 | Aylık işlem tarzları ve haber kaynakları</b>",
           "H083 kapsamı: 34 haber sonrası işlem varyasyonu + 10 hisse rotasyonu = 44 deney.",
           "3 haber ailesi: şirket büyük yatırımı; federal hibe/kredi/sözleşme; kanun/düzenleme.",
           "Haber seçime kaynak olabilir, tek başına fiyat etkisi veya kâr kanıtı değildir.",
           "Tarihsel H083: düşük beta +%60,09, ATR sıkışması +%51,75, SPY +%107,00. İstatistiksel alpha KANITLANMADI.",
           "İzlemede yalnız 2 aylık yöntem: düşük beta ve volatilite sıkışması.",
           ""]
    for row in new.to_dict("records"):
        rule=row["rule"]
        gate_row=gate[gate.rule.eq(rule)].iloc[0]
        approved=gate_row.status=="FORWARD_VALIDATED_PAPER"
        title="Düşük beta" if rule=="monthly_low_beta" else "Volatilite sıkışması"
        desc=("Beta 0–1, SMA200 üstü, 63g momentum pozitif, likidite ≥5M$" if
              rule=="monthly_low_beta" else
              "SMA50 üstü, ATR14/fiyat kendi 126g %20 eşiğinin altında, likidite ≥5M$")
        lines += [f"<b>{esc(title)}</b> · {'✅ İLERİ KAĞIT TESTİNDE DOĞRULANDI' if approved else '🟡 YALNIZCA İZLEME'}",
                  esc(desc),
                  f"İleri kayıt: {int(gate_row.closed_months)}/{MIN_MONTHS} ay · "
                  f"{int(gate_row.paper_positions)} kapanmış örnek · "
                  f"BH q={float(gate_row.bh_q):.3f}",
                  f"Sıradaki varsayımsal açılış: {esc(str(row['entry_session']))}",
                  f"Haberle ilişkili izleme hisseleri: {esc(', '.join(_parse_members(row)) or 'koşulu geçen yok')}"]
        for event in json.loads(row["events_json"])[:5]:
            lines.append(
                f"• <code>{esc(str(event['ticker']))}</code> "
                f"— haber: {esc(str(event['event_family']))} / {esc(str(event['category']))}"
            )
            url=str(event.get("source_url",""))
            if url.startswith("https://") and len(url)<=220:
                lines.append(f"  Kaynak: {esc(url)}")
        if approved:
            ready=plans[(plans.rule.eq(rule)) &
                        (plans.side.eq("BUY_NEXT_OPEN")) &
                        (plans.target_session.eq(row["entry_session"]))]
            if ready.empty:
                lines.append("⚠️ Kağıt doğrulama var; fiyat eksikliği nedeniyle işlem planı YOK.")
            else:
                lines.append("✅ <b>İLERİ KAĞIT TESTİNDE DOĞRULANMIŞ BİLGİ PLANI</b>")
                for item in ready.to_dict("records"):
                    qty=int(item["estimated_whole_shares"])
                    lines.append(
                        f"📋 <code>{esc(str(item['ticker']))}</code>: "
                        f"{esc(str(item['target_session']))} NYSE açılışında "
                        f"azami 1.990$ varsayımsal alış · "
                        f"son kapanış {float(item['indicative_last_close']):.2f}$ · "
                        f"tahmini {qty} tam adet · "
                        f"çıkış {esc(str(item['exit_session']))} NYSE açılışı."
                    )
                lines.append("Açılış fiyatı ve gerçek gerçekleşme bilinmiyor; broker emri YOK.")
        else:
            lines.append("⛔ <b>AL/SAT SİNYALİ DEĞİL.</b> Henüz onaylı işlem planı yok.")
        lines.append("")
    # Approved plan exits from a previous month remain due even if the research
    # gate has subsequently been suspended. Never suppress an exit reminder.
    upcoming=set(new.entry_session.astype(str))
    if not plans.empty:
        due=plans[plans.side.eq("EXIT_NEXT_MONTH_OPEN") &
                  plans.target_session.astype(str).isin(upcoming)]
        if not due.empty:
            lines.append("<b>📤 Önceden planlanan çıkışlar (bilgi amaçlı)</b>")
            for item in due.to_dict("records")[:10]:
                lines.append(
                    f"• <code>{esc(str(item['ticker']))}</code> "
                    f"{esc(str(item['target_session']))} NYSE açılışında "
                    f"önceden tanımlanmış kağıt pozisyon çıkışı; gerçek emir YOK."
                )
    lines += ["Not: Tam nakit portföy 10.000$, örnek pozisyon 1.990$, Midas varsayımı "
              "1,50$ alış + 1,50$ satış ve yön başına 5 baz puan kayma.",
              "Kaynak/veri eksikliği ayları başarısız kabul edilir; geçmiş H083 kârları ileri kanıta eklenmez.",
              "<i>Otomatik emir yoktur. Geçmiş/kağıt performansı gelecek getiri garantisi değildir.</i>"]
    return "\n".join(lines)


def _telegram_chunks(message: str, limit: int = 3500) -> list[str]:
    """Keep Telegram HTML lines whole; send limits include escaped markup."""
    chunks=[]
    current=[]
    current_len=0
    for line in message.split("\n"):
        if len(line)>limit:
            raise ValueError("Telegram report line exceeds safe HTML limit")
        additional=len(line)+(1 if current else 0)
        if current and current_len+additional>limit:
            chunks.append("\n".join(current))
            current=[]
            current_len=0
        current.append(line)
        current_len+=len(line)+(1 if len(current)>1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _notify(new: pd.DataFrame, gates: pd.DataFrame, plans: pd.DataFrame, now:datetime):
    if new.empty:
        return
    from . import telegram
    base="|".join(sorted(new.cohort_id.astype(str)))
    sent=_read("h083_telegram_sent",["message_key","sent_utc"])
    if not telegram.enabled():
        LOG.info("H083 monthly research bulletin ready; Telegram not configured")
        return
    for part_no,body in enumerate(_telegram_chunks(format_monthly_telegram(new,gates,plans))):
        key=f"{base}|part:{part_no}"
        if key in set(sent.message_key.astype(str)):
            continue
        if telegram.send(body):
            added=pd.DataFrame([{"message_key":key,
                                "sent_utc":pd.Timestamp(now).tz_convert("UTC").isoformat()}])
            store.upsert("h083_telegram_sent",added,"message_key")
            sent=pd.concat([sent,added],ignore_index=True)
        else:
            # The next same-session scheduled retry resumes at the failed part.
            break


def run(now_utc: datetime | None = None) -> dict:
    """At most once after today's *official* NYSE close, not by calendar guesswork."""
    now=now_utc or datetime.now(timezone.utc)
    today,schedule,index=_calendar(now)
    allowed,why=_active_session(now,today,schedule)
    if not allowed:
        from . import decision_audit
        decision_audit.record_forward_attempt(now, "SKIPPED", why, str(today.date()))
        print("[H083] skip: "+why)
        return {"skipped":why, "counts_as_forward_month":False}
    feed_path=store_dir()/"event_trade_feed.csv"
    if not feed_path.exists():
        raise FileNotFoundError("No event feed, cannot perform forward H083 decisions")
    feed=pd.read_csv(feed_path,low_memory=False)
    eligible=W.eligible_events(feed,now)
    if not eligible.empty:
        eligible=eligible.sort_values(["event_date","_known_sort","ticker"],ascending=False)
    capped=eligible.head(W.MAX_TICKERS)
    cohorts=_read("h083_forward_cohorts",COHORT_COLS)
    old_fills=_read("h083_forward_fills",FILL_COLS)
    old_results=_read("h083_forward_results",RESULT_COLS)
    old_plans=_read("h083_action_plans",PLAN_COLS)
    universe=sorted({"SPY"}|set(capped.ticker.astype(str))|set(_pending_tickers(cohorts,today)))
    bars=W.download_daily_bars(universe,period="3y")
    benchmark=W._regular_bars(bars.get("SPY",pd.DataFrame()))
    if today not in benchmark.index:
        raise ValueError(f"SPY completed OHLCV unavailable for {today.date()}, no decisions or fills")
    watch,exclusions,watch_status=W.evaluate_watch(capped,bars,now,asof=today)
    if watch_status.get("feature_asof")!=str(today.date()) or not watch_status.get("ok"):
        raise RuntimeError("Stale H083 candidate snapshot, no forward signal")
    # Whole-series Stooq fallback may have different adjustment conventions.
    # It can support WATCH research, but not irrevocable actual-open paper fills
    # or a supposedly statistically validated forward month.
    sourced=W.LAST_PRICE_REPORT.get("providers",{})
    execution_symbols={"SPY"}|set(_pending_tickers(cohorts,today))|set(
        watch.ticker.astype(str) if not watch.empty else [])
    unverified=[t for t in sorted(execution_symbols) if
                str(sourced.get(t,"")).startswith("STOOQ_")]
    if unverified:
        raise RuntimeError("H083_UNVERIFIED_SECONDARY_FILL_SOURCE: "+
                           ",".join(unverified[:8]))
    # Source metadata is an eligibility condition; all news subtypes are separately documented.
    store.write("event_watch_candidates",watch)
    store.write("event_watch_exclusions",exclusions)
    store.save_json("event_watch_status",watch_status)
    store.write("h083_style_catalog",C.catalog())
    store.write("h083_news_catalog",C.news_catalog(feed))
    cohorts,new=freeze_cohorts(today,index,watch,cohorts,now)
    # Fill existing AND today's brand-new cohorts. Never use later history to backfill.
    fills=capture_fills(today,now,cohorts,bars,old_fills)
    outcomes=resolve_months(now,today,cohorts,fills,old_results)
    gate=evidence_gate(outcomes,now)
    from . import research_assessment, decision_audit
    market=research_assessment.regime_snapshot(bars.get("SPY"),today)
    assessment=research_assessment.build_scorecard(outcomes,gate,watch_status,market,now)
    # A risk screen can only further RESTRICT information-only proposals.
    # Never alter monthly cohorts, realized paper results, or frozen evidence gates.
    risk_eligible=set(assessment.loc[
        assessment.research_decision.eq("WATCH_RESEARCH_ONLY"), "rule"].astype(str))
    plan_gate=gate.copy()
    if not plan_gate.empty:
        approved=plan_gate.validated_for_info_plans.astype(str).str.lower().isin(["true","1"])
        plan_gate["validated_for_info_plans"]=approved & plan_gate.rule.astype(str).isin(risk_eligible)
    plans=create_plans(today,now,new,watch,bars,plan_gate,old_plans)
    store.write("h083_forward_cohorts",cohorts)
    store.write("h083_forward_fills",fills)
    store.write("h083_forward_results",outcomes)
    store.write("h083_forward_gates",gate)
    store.write("h083_research_scorecard",assessment)
    store.save_json("h083_market_regime",market)
    store.write("h083_action_plans",plans)
    report={
        "ok":True,"generated_utc":pd.Timestamp(now).tz_convert("UTC").isoformat(),
        "last_complete_nyse_session":str(today.date()),"catalog_styles":44,
        "news_families":3,"monitor_rules":list(RULES),
        "research_approved_styles":0,
        "watch_candidates":len(watch),"cohorts_frozen_this_run":len(new),
        "forward_months_closed":len(outcomes),"approved_paper_rules":
            gate[gate.validated_for_info_plans].rule.astype(str).tolist(),
        "market_regime":market.get("regime","UNKNOWN"),
        "data_coverage_ok":watch_status.get("data_coverage_ok",False),
        "price_download":dict(W.LAST_PRICE_REPORT),
        "plans_eligible_after_risk":sorted(risk_eligible),
        "information_only":True,"broker_ordering":False,
        "note":"Historically positive H083 returns are not used for future validation.",
    }
    # The journal and operational attempt metadata distinguish successful
    # execution from a skipped Actions job and from statistical validation.
    payload={"feature_asof":str(today.date()),"candidates":len(watch),
             "new_cohorts":len(new),"completed_forward_months":len(outcomes),
             "approved_paper_rules":report["approved_paper_rules"],
             "regime":market.get("regime","UNKNOWN")}
    import hashlib
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:16]
    decision_audit.record("H083_FORWARD",
                          "forward:"+str(today.date())+":"+fingerprint,
                          "FORWARD_RESEARCH_ONLY",payload,observed_at_utc=now)
    decision_audit.record_forward_attempt(now,"COMPLETED_RESEARCH",
                                          "AFTER_NYSE_CLOSE",str(today.date()))
    store.save_json("h083_forward_status",report)
    if market.get("regime") == "HIGH_STRESS":
        try:
            from . import research_notifications
            research_notifications.warn_data_quality(
                "H083_MARKET_REGIME", "HIGH_STRESS",
                {"stress_ratio": market.get("stress_ratio", "UNKNOWN"),
                 "feature_asof": market.get("feature_asof", "UNKNOWN")})
        except Exception:
            LOG.warning("H083 market-stress notification unavailable")
    # Retry a same-day bulletin after transient Telegram failure, but never resend
    # a confirmed delivery or issue a late notification after the entry session.
    bulletin=cohorts[cohorts.signal_session.eq(str(today.date()))]
    if not bulletin.empty:
        _notify(bulletin,plan_gate,plans,now)
    print("[H083] "+json.dumps(report,ensure_ascii=False))
    return report


if __name__=="__main__":
    try:
        run()
    except Exception as exc:
        LOG.exception("H083 after-close job failed; no trade status promoted")
        try:
            from . import decision_audit
            decision_audit.record_forward_attempt(
                datetime.now(timezone.utc),"FAILED",type(exc).__name__)
        except Exception:
            LOG.exception("H083 failed attempt audit also failed")
        store.save_json("h083_forward_status",{
            "ok":False,"information_only":True,"broker_ordering":False,
            "last_attempt_utc":datetime.now(timezone.utc).isoformat(),
            "error_type":type(exc).__name__,"error":"İleri izleme veri/akış hatası; yeni karar yok."
        })
        raise
