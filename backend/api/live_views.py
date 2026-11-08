"""One live endpoint for the whole web UI (polled every 5 s by frontend/src/lib/LiveContext.jsx).

Everything that is shown live comes from here, so all pages and cards show the same values:
devices, latest mining/hardware/system values (as /api/mining/latest/, /api/hardware/latest/,
/api/devices/system-latest/), the throttle state per Avalon (as GET /api/devices/<id>/throttle/)
and the Temperatur-Automatik (as /api/fans/automation/). (The Takt-Messung of the A15 is removed since 06.11.)
Only the database is read - no miner and no AC Infinity access.
"""
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .fan_views import automation_payload
from .models import Device
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
    return Response({
        'server_now': timezone.now(),
        'devices': DeviceSerializer(devices, many=True).data,
        'mining': FleetMiningViewSet(request=request, format_kwarg=None).latest(request).data,
        'hardware': FleetHardwareViewSet(request=request, format_kwarg=None).latest(request).data,
        'system': device_view.system_latest(request).data,
        'throttle': throttle,
        'automation': automation_payload(),
    })
