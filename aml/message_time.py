"""Calendar hints from explicit source clocks; never from the receipt clock."""
import calendar
from datetime import datetime, timedelta
import re

RELATIVE = re.compile(r'昨天|今天|明天|(?:上|本|这|下)(?:周|星期)[一二三四五六日天]|(?:\d{1,2}|两|二|一)(?:个)?月前|\b(?:yesterday|today|tomorrow)\b|\b(?:last|this|next)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b|\b\d{1,2}\s+(?:days?|weeks?|months?)\s+ago\b', re.I)
DAYS = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']

def resolve(token, day):
 token=token.lower()
 simple={'昨天':-1,'今天':0,'明天':1,'yesterday':-1,'today':0,'tomorrow':1}
 if token in simple:return day+timedelta(days=simple[token])
 m=re.fullmatch(r'(last|this|next)\s+('+'|'.join(DAYS)+')',token)
 if m:
  weekday=DAYS.index(m[2])
  if m[1]=='last':return day-timedelta(days=(day.weekday()-weekday)%7 or 7)
  if m[1]=='next':return day+timedelta(days=(weekday-day.weekday())%7 or 7)
  return day+timedelta(days=weekday-day.weekday())
 m=re.fullmatch(r'(上|本|这|下)(?:周|星期)([一二三四五六日天])',token)
 if m:return day-timedelta(days=day.weekday())+timedelta(days={'上':-7,'本':0,'这':0,'下':7}[m[1]]+'一二三四五六日'.index(m[2].replace('天','日')))
 m=re.fullmatch(r'(\d{1,2})\s+(days?|weeks?|months?)\s+ago',token)
 if m:
  count=int(m[1]);unit=m[2]
  if unit.startswith('day'):return day-timedelta(days=count)
  if unit.startswith('week'):return day-timedelta(days=7*count)
 else:
  m=re.fullmatch(r'(\d{1,2}|两|二|一)(?:个)?月前',token)
  if not m:return None
  count=int(m[1]) if m[1].isdigit() else {'两':2,'二':2,'一':1}[m[1]]
 year,month=divmod(day.year*12+day.month-1-count,12)
 return day.replace(year=year,month=month+1,day=min(day.day,calendar.monthrange(year,month+1)[1]))

def annotate(rows):
 for row in rows:
  if row.get('message_time_origin') not in ('source','unknown'):continue
  matches=list(RELATIVE.finditer(str(row.get('text') or '')))
  if not matches:continue
  stamp=row.get('created_at');day=None
  if row['message_time_origin']=='source' and isinstance(stamp,str):
   try:
    dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if dt.tzinfo is not None:day=dt.date()
   except ValueError:pass
  notes=[]
  for m in matches:
   try:date=resolve(m.group(),day) if day else None
   except (ValueError,OverflowError):date=None
   notes.append({'expression':m.group(),'date':date.isoformat() if date else None,'basis':'source_message_date' if date else 'unknown'})
  row['relative_date_notes']=notes
 return rows
