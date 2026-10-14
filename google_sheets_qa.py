#!/usr/bin/env python3
"""
google_sheets_qa.py - Hermes QA Test Case Generator & Google Sheets Sync Module
Maps directly to the enterprise format of 'Skenario Testing - Sprint PNRI.xlsx'
"""

import os
import sys
import json
from datetime import datetime
from google_sheets_auth import get_sheets_service

# Kolom standar sesuai template 'Skenario Testing - Sprint PNRI.xlsx' (Baris 10)
# [1: No, 2: Created At, 3: Author, 4: Test Case ID, 5: Platform, 6: Modul, 7: Test Case, 
#  8: Type, 9: Pre-conditions, 10: Test Steps, 11: Expected Result, 12: Blank, 13: Status DEV, 
#  14: Evidence DEV, 15: Blank, 16: Status UAT, ...]

def format_tc_row(index, tc, author="Aris"):
    """
    Mengubah objek Test Case menjadi baris array yang sesuai template Skenario Testing.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    no = index
    created_at = today_str
    author_name = author
    tc_id = tc.get("id") or tc.get("code") or f"TC-YAP-{index:03d}"
    platform = tc.get("platform", "Mbl-Android")
    module = tc.get("module", "Billing & Payment")
    title = tc.get("title") or tc.get("name") or tc.get("scenario", "")
    tc_type = tc.get("type", "Positive")
    preconditions = tc.get("preconditions") or "User sudah login, build Sprint 6 terinstall"
    steps = tc.get("steps") or ""
    if isinstance(steps, list):
        steps = "\n".join([f"{i+1}. {s}" for i, s in enumerate(steps)])
    expected = tc.get("expected") or ""
    status_dev = tc.get("status_dev", "Not Tested")
    evidence_dev = tc.get("notes") or tc.get("evidence", "Generated via Hermes QA")
    status_uat = "Not Tested"
    
    # 34 Kolom sesuai template lengkap
    row = [
        no,                             # Col 1: No
        created_at,                     # Col 2: Created At
        author_name,                    # Col 3: Author
        tc_id,                          # Col 4: Test Case ID
        platform,                       # Col 5: Platform
        module,                         # Col 6: Modul
        title,                          # Col 7: Test Case
        tc_type,                        # Col 8: Type
        preconditions,                  # Col 9: Pre-conditions
        steps,                          # Col 10: Test Steps
        expected,                       # Col 11: Expected Result
        "NOT YET",                      # Col 12: Progress indicator
        status_dev,                     # Col 13: Status DEV
        evidence_dev,                   # Col 14: Evidence DEV
        "",                             # Col 15: Separator
        status_uat,                     # Col 16: Status UAT
        "",                             # Col 17: Evidence UAT
        "",                             # Col 18: Status PROD
        "",                             # Col 19: Evidence PROD
        "Yes",                          # Col 20: Functional
        "Yes" if tc_type == "Negative" else "No", # Col 21: Negative
        "Yes",                          # Col 22: Data Integrity
        "Yes" if "boundary" in tc_type.lower() else "No", # Col 23: Boundary
        "No",                           # Col 24: Security
        "No",                           # Col 25: Performance
        6,                              # Col 26: QA Checked
        "100%",                         # Col 27: QA Score
        tc.get("likelihood", "Moderate"),# Col 28: Likelihood
        tc.get("impact", "Major"),       # Col 29: Impact
        tc.get("risk_level", "Medium"),  # Col 30: Risk Level
        tc.get("risk_point", 6),         # Col 31: Risk Point
        "Ops",                          # Col 32: Business Impact
        2,                              # Col 33: Impact Points
        "Medium - Ops"                  # Col 34: Combined Risk
    ]
    return row


def load_existing_test_cases(excel_path="/home/ares/Documents/myserver/Skenario Testing - Sprint PNRI.xlsx"):
    """
    Membaca 71 test case eksisting dari template/file Excel Sprint PNRI.
    """
    import openpyxl
    if not os.path.exists(excel_path):
        return []
    
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb.active
    tcs = []
    current_section = ""
    for r in range(11, ws.max_row + 1):
        c1 = ws.cell(r, 1).value
        c4 = ws.cell(r, 4).value # TC ID
        c6 = ws.cell(r, 6).value # Modul
        c7 = ws.cell(r, 7).value # Title
        c8 = ws.cell(r, 8).value # Type
        c9 = ws.cell(r, 9).value # Preconditions
        c10 = ws.cell(r, 10).value # Steps
        c11 = ws.cell(r, 11).value # Expected
        c13 = ws.cell(r, 13).value # Status DEV
        
        if c1 and isinstance(c1, str) and not c4:
            current_section = c1.strip()
            continue
            
        if c4 or c7:
            tcs.append({
                "row": r,
                "no": c1,
                "section": current_section,
                "id": str(c4 or "").strip(),
                "module": str(c6 or "").strip(),
                "title": str(c7 or "").strip(),
                "type": str(c8 or "Positive").strip(),
                "preconditions": str(c9 or "").strip(),
                "steps": str(c10 or "").strip(),
                "expected": str(c11 or "").strip(),
                "status_dev": str(c13 or "Not Tested").strip()
            })
    return tcs


def sync_extracted_test_cases(spreadsheet_id, sheet_name, test_cases, author="Aris"):
    """
    Menyinkronkan daftar test case yang diekstrak dari notulen meeting:
    - Jika action == 'UPDATE': Mencari baris dengan ID atau Judul yang sama dan memperbarui Expected / Notes / Steps.
    - Jika action == 'CREATE': Menambahkan baris baru ke akhir spreadsheet.
    """
    service = get_sheets_service()
    
    # 1. Ambil baris yang ada untuk pencocokan ID
    range_all = f"'{sheet_name}'!A11:N120"
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_all
    ).execute()
    sheet_rows = result.get('values', [])
    
    updated_count = 0
    created_list = []
    
    for tc in test_cases:
        action = (tc.get("action") or "CREATE").upper()
        target_id = tc.get("target_id") or tc.get("id") or ""
        
        if action == "UPDATE" and target_id:
            # Cari baris yang sesuai (Kolom D = index 3)
            matched_indices = []
            for idx, row in enumerate(sheet_rows):
                row_tc_id = row[3].strip() if len(row) > 3 and row[3] else ""
                if row_tc_id == target_id:
                    matched_indices.append(idx + 11) # Baris di sheet (1-based)
            
            if matched_indices:
                today_str = datetime.now().strftime("%Y-%m-%d")
                notes = tc.get("notes") or tc.get("evidence") or "Update via Notulen Meeting"
                expected = tc.get("expected") or ""
                
                for row_num in matched_indices:
                    update_range = f"'{sheet_name}'!K{row_num}:N{row_num}"
                    body = {
                        'values': [[
                            expected,
                            "UPDATED",
                            tc.get("status_dev", "Not Tested"),
                            f"[{today_str} Rapat] {notes}"
                        ]]
                    }
                    service.spreadsheets().values().update(
                        spreadsheetId=spreadsheet_id,
                        range=update_range,
                        valueInputOption="USER_ENTERED",
                        body=body
                    ).execute()
                    updated_count += 1
            else:
                # Jika tidak ditemukan barisnya, masukkan sebagai create
                created_list.append(tc)
        else:
            created_list.append(tc)
            
    if created_list:
        append_test_cases_to_sheet(spreadsheet_id, sheet_name, created_list, author=author)
        
    return {
        "updated": updated_count,
        "created": len(created_list),
        "total": updated_count + len(created_list)
    }


def append_test_cases_to_sheet(spreadsheet_id, sheet_name, test_cases, author="Aris"):
    """
    Menambahkan list test case ke spreadsheet target pada sheet_name tertentu.
    """
    service = get_sheets_service()
    
    # 1. Cek baris terakhir yang terisi
    range_check = f"'{sheet_name}'!A:A"
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_check
    ).execute()
    existing_rows = result.get('values', [])
    start_row = len(existing_rows) + 1
    
    # Hitung nomor urut lanjutannya
    start_no = 1
    for r in reversed(existing_rows):
        if r and str(r[0]).isdigit():
            start_no = int(r[0]) + 1
            break
            
    # Siapkan rows
    rows_to_append = []
    for idx, tc in enumerate(test_cases):
        rows_to_append.append(format_tc_row(start_no + idx, tc, author=author))
        
    target_range = f"'{sheet_name}'!A{start_row}"
    body = {
        'values': rows_to_append
    }
    
    res = service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=target_range,
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()
    
    print(f"[SUCCESS] Berhasil menambahkan {len(rows_to_append)} Test Case ke sheet '{sheet_name}'!")
    return res


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Penggunaan: python3 google_sheets_qa.py <SPREADSHEET_ID> <SHEET_NAME>")
        print("Contoh: python3 google_sheets_qa.py 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms 'Sprint 6 PNRI'")
        sys.exit(1)
        
    # Contoh dry-run dummy
    sample_tcs = [
        {
            "id": "PNRI-010",
            "module": "Pembayaran VA",
            "title": "Verifikasi pembayaran nomor VA PNRI dengan saldo Yap! mencukupi",
            "type": "Positive",
            "preconditions": "User telah login dan memiliki tagihan VA aktif",
            "steps": ["Buka menu VA PNRI", "Input nomor VA 8808123456", "Konfirmasi pembayaran"],
            "expected": "Transaksi sukses, struk pembayaran tampil, tagihan berstatus lunas"
        }
    ]
    append_test_cases_to_sheet(sys.argv[1], sys.argv[2], sample_tcs)
