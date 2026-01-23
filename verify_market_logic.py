from datetime import datetime

def is_market_closed():
    """
    Piyasa Kapalı mı kontrolü (Türkiye saati varsayımıyla):
    Kapanış: Cumartesi 01:00
    Açılış: Pazartesi 02:00
    """
    now = datetime.now()
    print(f"Current time: {now}")
    weekday = now.weekday() # 0: Pzt, 6: Paz
    print(f"Weekday: {weekday}")
    hour = now.hour
    print(f"Hour: {hour}")
    
    # Cumartesi (5)
    if weekday == 5:
        return hour >= 1
    # Pazar (6)
    if weekday == 6:
        return True
    # Pazartesi (0)
    if weekday == 0:
        return hour < 2
        
    return False

if __name__ == "__main__":
    status = is_market_closed()
    print(f"Is market closed? {status}")
    if not status:
        print("PASS: Market is OPEN as expected for Friday.")
    else:
        print("FAIL: Market should be OPEN for Friday.")
