import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta

# ---------------------------------------------------------
# 1. KONFIGURASI HALAMAN
# ---------------------------------------------------------
st.set_page_config(page_title="BitTest Pro (Intraday + SMA)", layout="wide")

# Sidebar (Menu Kiri)
st.sidebar.header("⚙️ Pengaturan Strategi")
ticker = st.sidebar.text_input("Simbol Aset", value="BTC-USD")

# --- UPDATE: TIME FRAME LENGKAP ---
st.sidebar.caption("Pengaturan Waktu")
tf_options = ["1 Jam (1h)", "4 Jam (4h)", "Harian (1d)", "Mingguan (1wk)", "Bulanan (1mo)"]
tf_choice = st.sidebar.selectbox("Time Frame", tf_options, index=2) # Default Harian

# Mapping pilihan ke interval yfinance
if "1 Jam" in tf_choice:
    interval_yf = "1h"
    is_intraday = True
    resample_4h = False
elif "4 Jam" in tf_choice:
    interval_yf = "1h" # Kita ambil 1h lalu di-resample jadi 4h
    is_intraday = True
    resample_4h = True
elif "Harian" in tf_choice:
    interval_yf = "1d"
    is_intraday = False
    resample_4h = False
elif "Mingguan" in tf_choice:
    interval_yf = "1wk"
    is_intraday = False
    resample_4h = False
else: # Bulanan
    interval_yf = "1mo"
    is_intraday = False
    resample_4h = False

# Input Tanggal (Perlu penyesuaian logika untuk Intraday)
if is_intraday:
    # Intraday (1h/4h) di Yahoo Finance max 730 hari. 
    # Kita set default start date lebih dekat agar tidak error.
    default_start = datetime.now() - timedelta(days=365) # Default 1 tahun lalu
    st.sidebar.info("⚠️ Data 1 Jam/4 Jam di Yahoo Finance terbatas maks 730 hari (2 tahun) ke belakang.")
else:
    default_start = pd.to_datetime("2020-01-01")

start_date = st.sidebar.date_input("Mulai Tanggal", value=default_start)
modal_awal = st.sidebar.number_input("Modal Awal ($)", value=10000)

st.sidebar.divider()
st.sidebar.subheader("🔧 Parameter SMA")

sma_period = st.sidebar.number_input(
    "Periode Moving Average (SMA)", 
    value=50, 
    min_value=5, 
    step=5,
    help="Garis rata-rata N candle terakhir."
)

st.sidebar.caption("Manajemen Risiko")
stop_loss_pct = st.sidebar.number_input("Stop Loss (%)", value=5.0, step=0.5)

btn_run = st.sidebar.button("🚀 Jalankan Backtest", type="primary")

# ---------------------------------------------------------
# 2. LOGIKA UTAMA
# ---------------------------------------------------------
def ambil_data(symbol, start_req, interval, need_resample_4h, period_sma):
    # Buffer logic
    # Jika Intraday, buffer tidak boleh terlalu jauh melebihi 730 hari
    if interval == '1h':
        days_buffer = (period_sma / 24) + 10 # Buffer secukupnya untuk hitung SMA awal
        # Pastikan start_buffer tidak menabrak limit 730 hari Yahoo
        limit_date = datetime.now() - timedelta(days=729)
        start_buffer = pd.to_datetime(start_req) - timedelta(days=days_buffer)
        
        if start_buffer < limit_date:
            start_buffer = limit_date
    elif interval == '1wk':
        days_buffer = (period_sma * 7) + 365 
        start_buffer = pd.to_datetime(start_req) - timedelta(days=days_buffer)
    elif interval == '1mo':
        days_buffer = (period_sma * 30) + 365
        start_buffer = pd.to_datetime(start_req) - timedelta(days=days_buffer)
    else: # Daily
        days_buffer = period_sma + 365
        start_buffer = pd.to_datetime(start_req) - timedelta(days=days_buffer)

    # Download Data
    try:
        df = yf.download(symbol, start=start_buffer, interval=interval, progress=False)
    except Exception as e:
        st.error(f"Gagal mengambil data: {e}")
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    
    # --- LOGIKA RESAMPLING 4H ---
    if need_resample_4h and not df.empty:
        # Aturan konversi 1H -> 4H
        logic = {
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }
        # Resample data
        df = df.resample('4h').apply(logic)
        df.dropna(inplace=True) # Hapus baris kosong akibat resample
        
    df.dropna(inplace=True)
    return df

def hitung_sma(df, period):
    label_sma = f'SMA_{period}'
    df[label_sma] = df['Close'].rolling(window=period).mean()
    return df, label_sma

def jalankan_engine(df, start_date_user, initial_capital, col_sma_name, sl_percent):
    # Filter tanggal
    # Konversi start_date_user ke datetime yg timezone aware jika perlu, tapi sederhananya:
    start_ts = pd.to_datetime(start_date_user).tz_localize(None)
    
    # Hapus timezone dari index df agar compatible saat filtering
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
        
    df_sim = df[df.index >= start_ts].copy()
    
    if df_sim.empty:
        return df_sim, []

    balance = initial_capital
    btc_amount = 0
    entry_price = 0
    trades = []
    equity_curve = []
    
    sl_decimal = sl_percent / 100.0
    
    for date, row in df_sim.iterrows():
        price = row['Close']
        sma_val = row[col_sma_name]
        low_price = row['Low']
        
        if pd.isna(sma_val):
            equity_curve.append(balance + (btc_amount * price))
            continue

        # LOGIKA BELI (Entry)
        if btc_amount == 0 and price > sma_val:
            btc_amount = balance / price
            entry_price = price
            balance = 0
            trades.append({
                "Tanggal": date, 
                "Tipe": "BELI 🟢", 
                "Harga": price, 
                "SMA": sma_val,
                "Profit %": 0, 
                "Saldo": entry_price * btc_amount
            })
            
        # LOGIKA JUAL (Exit)
        elif btc_amount > 0:
            current_pnl_pct = ((price - entry_price) / entry_price) * 100
            stop_loss_price = entry_price * (1 - sl_decimal)
            
            # 1. Stop Loss
            if low_price <= stop_loss_price:
                sell_val = btc_amount * stop_loss_price
                balance = sell_val
                btc_amount = 0
                trades.append({
                    "Tanggal": date, 
                    "Tipe": "CUT LOSS ✂️", 
                    "Harga": stop_loss_price, 
                    "SMA": sma_val,
                    "Profit %": -sl_percent, 
                    "Saldo": balance
                })
                
            # 2. SMA Exit
            elif price < sma_val:
                sell_val = btc_amount * price
                balance = sell_val
                btc_amount = 0
                trades.append({
                    "Tanggal": date, 
                    "Tipe": "JUAL 🔴", 
                    "Harga": price, 
                    "SMA": sma_val,
                    "Profit %": current_pnl_pct, 
                    "Saldo": balance
                })
        
        current_val = balance + (btc_amount * price)
        equity_curve.append(current_val)
    
    df_sim['Equity'] = equity_curve
    return df_sim, trades

# ---------------------------------------------------------
# 3. USER INTERFACE
# ---------------------------------------------------------
st.title(f"📈 Backtest SMA ({tf_choice}): {ticker}")

if btn_run:
    with st.spinner(f'Mengambil data {tf_choice} & menghitung SMA {sma_period}...'):
        try:
            # A. Ambil Data (Handle resample logic inside)
            raw_data = ambil_data(ticker, start_date, interval_yf, resample_4h, sma_period)
            
            if raw_data.empty:
                st.error("Data kosong! Untuk Timeframe 1h/4h, pastikan tanggal mulai tidak lebih dari 730 hari lalu.")
            else:
                # B. Hitung SMA
                data_indi, nama_kolom_sma = hitung_sma(raw_data, sma_period)
                
                # C. Engine
                result_df, trade_log = jalankan_engine(data_indi, start_date, modal_awal, nama_kolom_sma, stop_loss_pct)
                
                if result_df.empty:
                    st.warning(f"Data tersedia, tapi kosong setelah filter tanggal {start_date}. Geser tanggal lebih baru.")
                else:
                    # D. Statistik
                    final_balance = result_df['Equity'].iloc[-1]
                    profit_usd = final_balance - modal_awal
                    profit_pct = (profit_usd / modal_awal) * 100
                    
                    closed_trades = [t for t in trade_log if "BELI" not in t['Tipe']]
                    win_trades = [t for t in closed_trades if t['Profit %'] > 0]
                    win_rate = (len(win_trades) / len(closed_trades) * 100) if closed_trades else 0

                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Saldo Akhir", f"${final_balance:,.2f}")
                    c2.metric("Total Profit", f"{profit_pct:.2f}%", f"${profit_usd:,.0f}")
                    c3.metric("Total Transaksi", len(closed_trades))
                    c4.metric(f"Win Rate", f"{win_rate:.1f}%")

                    # E. Grafik
                    st.subheader(f"Chart: {ticker} ({tf_choice})")
                    fig = go.Figure()
                    
                    # Harga
                    fig.add_trace(go.Scatter(
                        x=result_df.index, y=result_df['Close'],
                        mode='lines', name='Close Price',
                        line=dict(color='white', width=1)
                    ))
                    
                    # SMA
                    fig.add_trace(go.Scatter(
                        x=result_df.index, y=result_df[nama_kolom_sma],
                        mode='lines', name=f'SMA {sma_period}',
                        line=dict(color='#ffff00', width=2)
                    ))
                    
                    # Marker
                    buy_d = [t['Tanggal'] for t in trade_log if "BELI" in t['Tipe']]
                    buy_p = [t['Harga'] for t in trade_log if "BELI" in t['Tipe']]
                    sell_d = [t['Tanggal'] for t in trade_log if "BELI" not in t['Tipe']]
                    sell_p = [t['Harga'] for t in trade_log if "BELI" not in t['Tipe']]

                    fig.add_trace(go.Scatter(
                        x=buy_d, y=buy_p, mode='markers', name='Buy',
                        marker=dict(symbol='triangle-up', size=10, color='green')
                    ))
                    fig.add_trace(go.Scatter(
                        x=sell_d, y=sell_p, mode='markers', name='Sell',
                        marker=dict(symbol='triangle-down', size=10, color='red')
                    ))

                    fig.update_layout(template="plotly_dark", height=600, margin=dict(t=30, b=20))
                    st.plotly_chart(fig, use_container_width=True)

                    # F. Tabel
                    st.subheader("Riwayat Transaksi")
                    if len(trade_log) > 0:
                        df_log = pd.DataFrame(trade_log)
                        
                        df_log['Harga'] = df_log['Harga'].map('${:,.2f}'.format)
                        df_log['SMA'] = df_log['SMA'].map('${:,.2f}'.format)
                        df_log['Saldo'] = df_log['Saldo'].map('${:,.2f}'.format)
                        df_log['Profit %'] = df_log['Profit %'].map('{:,.2f}%'.format)
                        # Format tanggal agar jam terlihat jika intraday
                        if is_intraday:
                            df_log['Tanggal'] = df_log['Tanggal'].dt.strftime('%Y-%m-%d %H:%M')
                        else:
                            df_log['Tanggal'] = df_log['Tanggal'].dt.strftime('%Y-%m-%d')
                        
                        st.dataframe(df_log, use_container_width=True)
                    else:
                        st.info("Tidak ada transaksi.")

        except Exception as e:
            st.error(f"Error System: {e}")
else:
    st.info("👈 Pilih Time Frame (termasuk 1h/4h) & SMA di sidebar.")