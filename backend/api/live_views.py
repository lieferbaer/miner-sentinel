"""One live endpoint for the whole web UI (polled every 5 s by frontend/src/lib/LiveContext.jsx).

Everything that is shown live comes from here, so all pages and cards show the same values:
devices, latest mining/hardware/system values (as /api/mining/latest/, /api/hardware/latest/,
/api/devices/system-latest/), the throttle state per Avalon (as GET /api/devices/<id>/throttle/),
the Temperatur-Automatik (as /api/fans/automation/) and the current Takt-Tabellen run of the A15.
Only the database is read - no miner and no AC Infinity access.
"""
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .fan_views import _a15, automation_payload
from .models import CalibrationRun, Device
from .serializers import DeviceSerializer


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def live(request):
    from .views import DeviceViewSet, FleetHardwareViewSet, FleetMiningViewSet

    devices = Device.objects.all().order_by('make', 'name')
    device_view = DeviceViewSet(request=request, format_kwarg=None)
    throttle = {}
    for dev in devices.filter(is_active=True, protocol=Device.PROTOCOL_CGMINER_TCP):
        seen = DeviceViewSet._throttle_seen(dev)
        throttle[dev.id] = (device_view._throttle_payload(dev, seen) if seen and seen['supported']
                            else {'supported': False})
    a15 = _a15()
    run = CalibrationRun.objects.filter(device=a15).order_by('-created_at').first() if a15 else None
    return Response({
        'server_now': timezone.now(),
        'devices': DeviceSerializer(devices, many=True).data,
        'mining': FleetMiningViewSet(request=request, format_kwarg=None).latest(request).data,
        'hardware': FleetHardwareViewSet(request=request, format_kwarg=None).latest(request).data,
        'system': device_view.system_latest(request).data,
        'throttle': throttle,
        'automation': automation_payload(),
        'calibration': {'device_id': a15.id if a15 else None, 'run': device_view._cal_run(run) if run else None},
    })
